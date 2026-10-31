"""Owned inputs and closed outcomes for authoring prompt-guard services."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Final, Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.contracts.readonly_json import JsonValue

PromptGuardRole = Literal["composer", "advisor"]
PromptGuardMode = Literal["required", "debug-when-unconfigured"]
PromptGuardDisposition = Literal["allow", "blocked", "unavailable", "cancelled", "debug_bypass"]
PromptGuardReason = Literal[
    "clean", "policy_blocked", "service_error", "timeout", "invalid_response", "unsupported_input", "cancelled", "guard_not_configured"
]

PROMPT_GUARD_TRUST_KEY = "_elspeth_prompt_guard_trust"
PROMPT_GUARD_CONTENT_PARTS_KEY = "_elspeth_prompt_guard_content_parts"
PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY = "_elspeth_prompt_guard_tool_argument_parts"
PROMPT_GUARD_THINKING_PARTS_KEY = "_elspeth_prompt_guard_thinking_parts"
# The complete set of message keys carrying producer provenance rather than wire
# content. Every consumer that separates the two derives its keys from here, so
# a new key cannot be honoured in one place and missed in another, where the
# miss would fail open.
PROMPT_GUARD_PROVENANCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        PROMPT_GUARD_TRUST_KEY,
        PROMPT_GUARD_CONTENT_PARTS_KEY,
        PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
        PROMPT_GUARD_THINKING_PARTS_KEY,
    }
)
# Every provenance key that carries a partition of the message rather than a
# whole-message verdict, in a stable order. Derived, so a new partition key is
# admission-checked below without an edit; sorted, because frozenset order
# follows the hash seed and which key is refused first must not.
PROMPT_GUARD_PARTITION_KEYS: Final[tuple[str, ...]] = tuple(sorted(PROMPT_GUARD_PROVENANCE_KEYS - {PROMPT_GUARD_TRUST_KEY}))
# Azure Prompt Shields limits each physical call, not the complete authoring
# history. Longer inspection runs are sent in as many calls as required.
# https://learn.microsoft.com/en-us/azure/ai-services/content-safety/region-availability#service-limits
PROMPT_GUARD_MAX_TEXT_CHARACTERS = 10_000
PROMPT_GUARD_MAX_DOCUMENTS_PER_CALL = 5


class PromptGuardTrust(Enum):
    """In-process provenance minted by owned builders, never decoded from JSON."""

    INSTRUCTION = "operator_instruction"
    REFERENCE = "operator_reference"


@dataclass(frozen=True, slots=True)
class PromptGuardContentPart:
    """One complete producer-rendered unit, untrusted by default.

    ``text`` covers exact wire bytes. Owned serializers may separately carry
    decoded ``inspection_text`` so the detector reads the original words;
    an absent projection inspects ``text`` directly. Neither grants trust.
    """

    text: str = field(repr=False)
    trust: PromptGuardTrust | None = None
    inspection_text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.text) is not str
            or (self.trust is not None and type(self.trust) is not PromptGuardTrust)
            or (self.inspection_text is not None and type(self.inspection_text) is not str)
        ):
            raise AuditIntegrityError("Prompt guard content part lacks exact owned provenance")


@dataclass(frozen=True, slots=True)
class PromptGuardToolArgumentParts:
    """Lossless untrusted units of one already-decoded tool argument string."""

    tool_call_index: int
    parts: tuple[PromptGuardContentPart, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.tool_call_index) is not int
            or self.tool_call_index < 0
            or type(self.parts) is not tuple
            or any(type(part) is not PromptGuardContentPart or part.trust is not None for part in self.parts)
        ):
            raise AuditIntegrityError("Prompt guard tool arguments require owned untrusted semantic units")


@dataclass(frozen=True, slots=True)
class PromptGuardThinkingParts:
    """Lossless untrusted inspection of one provider-native thinking field."""

    parts: tuple[PromptGuardContentPart, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.parts) is not tuple or any(type(part) is not PromptGuardContentPart or part.trust is not None for part in self.parts):
            raise AuditIntegrityError("Provider thinking partitions require owned untrusted units")


type PromptGuardTextMessage = dict[str, str | PromptGuardTrust | tuple[PromptGuardContentPart, ...]]
type GuardedProviderMessage = Mapping[
    str,
    JsonValue | PromptGuardTrust | tuple[PromptGuardContentPart, ...] | tuple[PromptGuardToolArgumentParts, ...] | PromptGuardThinkingParts,
]


@dataclass(slots=True)
class DetachedPromptGuardEvidence:
    """Mutable detached wire values and separate, immutable producer evidence.

    SDK preparation needs ordinary lists and dictionaries. The transport
    envelope therefore stays mutable; only its producer provenance values
    promise immutability, and coverage is revalidated before dispatch.
    """

    messages: list[dict[str, Any]] = field(repr=False)
    trusted_message_indexes: frozenset[int]
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] = field(repr=False)
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] = field(repr=False)
    thinking_parts: tuple[PromptGuardThinkingParts | None, ...] = field(default=(), repr=False)


def _parts_cover_text(parts: tuple[PromptGuardContentPart, ...], text: str) -> bool:
    offset = 0
    for part in parts:
        if not text.startswith(part.text, offset):
            return False
        offset += len(part.text)
    return offset == len(text)


def _standard_json_chunks(value: object) -> Iterator[str]:
    """Preserve native provenance's standard JSON spelling without joined text.

    Strings are escaped in bounded pieces.  This deliberately uses the existing
    standard JSON numeric/key spelling, not the aggregate RFC 8785 convention.
    Actual native blocks have their separate closed grammar admission.
    """
    active: set[int] = set()

    def string(text: str) -> Iterator[str]:
        yield '"'
        for start in range(0, str.__len__(text), 4096):
            chunk = str.__getitem__(text, slice(start, start + 4096))
            yield json.dumps(chunk, ensure_ascii=False, allow_nan=False)[1:-1]
        yield '"'

    def visit(node: object) -> Iterator[str]:
        if isinstance(node, str):
            yield from string(node)
        elif node is None or isinstance(node, (bool, int, float)):
            yield json.dumps(node, ensure_ascii=False, allow_nan=False)
        elif isinstance(node, (dict, MappingProxyType, list, tuple)):
            identity = id(node)
            if identity in active:
                raise ValueError("Circular reference detected")
            active.add(identity)
            try:
                if isinstance(node, (dict, MappingProxyType)):
                    yield "{"
                    first = True
                    for key, item in sorted(node.items()):
                        if not first:
                            yield ","
                        first = False
                        if isinstance(key, str):
                            yield from string(key)
                        elif key is None or isinstance(key, (bool, int, float)):
                            yield from string(json.dumps(key, allow_nan=False))
                        else:
                            raise TypeError("Native JSON object keys are not supported scalars")
                        yield ":"
                        yield from visit(item)
                    yield "}"
                else:
                    yield "["
                    for index, item in enumerate(node):
                        if index:
                            yield ","
                        yield from visit(item)
                    yield "]"
            finally:
                active.remove(identity)
        else:
            raise TypeError("Native JSON field contains an unsupported value")

    yield from visit(value)


def _parts_cover_chunks(parts: tuple[PromptGuardContentPart, ...], chunks: Iterable[str]) -> bool:
    part_index = 0
    part_offset = 0
    matches = True
    for chunk in chunks:
        if not matches:
            # Serialization failures anywhere in the complete field retain
            # precedence over a partition mismatch, as with the prior dumps.
            continue
        offset = 0
        while offset < len(chunk):
            while part_index < len(parts) and part_offset == len(parts[part_index].text):
                part_index += 1
                part_offset = 0
            if part_index == len(parts):
                matches = False
                break
            text = parts[part_index].text
            length = min(len(chunk) - offset, len(text) - part_offset)
            if not text.startswith(chunk[offset : offset + length], part_offset, part_offset + length):
                matches = False
                break
            offset += length
            part_offset += length
    while part_index < len(parts) and part_offset == len(parts[part_index].text):
        part_index += 1
        part_offset = 0
    return matches and part_index == len(parts)


def validate_prompt_guard_messages(messages: Sequence[object]) -> None:
    """Validate borrowed producer evidence before request-owned detachment.

    Only one message position is wrapped at a time.  Neither wire containers
    nor concatenated content/tool/native partition strings are materialized.
    The validation families keep the existing markers/content/arguments/native
    precedence of ``detach_prompt_guard_evidence``.
    """
    for message in messages:
        if not isinstance(message, Mapping):
            raise AuditIntegrityError("Prompt guard input requires complete owned message mappings")
        if PROMPT_GUARD_TRUST_KEY in message and type(message[PROMPT_GUARD_TRUST_KEY]) is not PromptGuardTrust:
            raise AuditIntegrityError("Prompt guard trust marker is not an owned producer identity")
        for key in PROMPT_GUARD_PARTITION_KEYS:
            if key in message and (message[key] is None or PROMPT_GUARD_TRUST_KEY in message):
                raise AuditIntegrityError(f"Prompt guard partition {key} conflicts with whole-message provenance")
    for message in messages:
        assert isinstance(message, Mapping)
        parts = message[PROMPT_GUARD_CONTENT_PARTS_KEY] if PROMPT_GUARD_CONTENT_PARTS_KEY in message else None
        validate_prompt_guard_content_parts((message,), (parts,))
    for message in messages:
        assert isinstance(message, Mapping)
        arguments = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
        validate_prompt_guard_tool_argument_parts((message,), (arguments,))
    for message in messages:
        assert isinstance(message, Mapping)
        thinking = message[PROMPT_GUARD_THINKING_PARTS_KEY] if PROMPT_GUARD_THINKING_PARTS_KEY in message else None
        validate_prompt_guard_thinking_parts((message,), (thinking,))


def validate_prompt_guard_content_parts(
    messages: Sequence[Mapping[str, Any]], content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...]
) -> None:
    """Reject foreign, missing or overlapping coverage before interpreting trust."""
    if type(content_parts) is not tuple or len(content_parts) != len(messages):
        raise AuditIntegrityError("Prompt guard content partition lacks exact message positions")
    for message, parts in zip(messages, content_parts, strict=True):
        if parts is None:
            continue
        if type(parts) is not tuple or any(type(part) is not PromptGuardContentPart for part in parts):
            raise AuditIntegrityError("Prompt guard content partition is not an owned producer identity")
        if "content" not in message or type(message["content"]) is not str or not _parts_cover_text(parts, message["content"]):
            raise AuditIntegrityError("Prompt guard content partition does not cover the complete wire content")


def thaw_guarded_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Thaw the transport wire and carry producer provenance through unchanged.

    SDK preparation needs ordinary lists and dictionaries, which is what
    ``deep_thaw`` produces: it converts every tuple to a list for JSON output.
    Thawing a whole message would therefore rewrite an owned provenance
    partition as a list, and the validators below would refuse the producer's
    own evidence as foreign. Only the wire is thawed here; each provenance
    value keeps the owned identity its producer minted, which is the split
    ``DetachedPromptGuardEvidence`` describes.
    """
    return [
        {key: value if key in PROMPT_GUARD_PROVENANCE_KEYS else deep_thaw(value) for key, value in message.items()} for message in messages
    ]


def detach_prompt_guard_evidence(messages: Sequence[Mapping[str, Any]]) -> DetachedPromptGuardEvidence:
    """Separate nominal producer provenance from detached, marker-free wire input.

    Call this before wire hashing, provider-context capture or dispatch. A
    string decoded from user/model JSON cannot mint trusted provenance.
    """
    validate_prompt_guard_messages(messages)
    wire: list[dict[str, Any]] = []
    trusted: set[int] = set()
    content_parts: list[tuple[PromptGuardContentPart, ...] | None] = []
    tool_argument_parts: list[tuple[PromptGuardToolArgumentParts, ...] | None] = []
    thinking_parts: list[PromptGuardThinkingParts | None] = []
    for index, message in enumerate(messages):
        if PROMPT_GUARD_TRUST_KEY in message:
            marker = message[PROMPT_GUARD_TRUST_KEY]
            if type(marker) is not PromptGuardTrust:
                raise AuditIntegrityError("Prompt guard trust marker is not an owned producer identity")
            trusted.add(index)
        # One rule over every partition key: a partition must carry parts, and
        # must not sit beside a whole-message verdict that would also cover
        # them. Derived from the canonical set, so a key added there is refused
        # here rather than stripped from the wire with nothing checking it.
        for key in PROMPT_GUARD_PARTITION_KEYS:
            if key in message and (message[key] is None or PROMPT_GUARD_TRUST_KEY in message):
                raise AuditIntegrityError(f"Prompt guard partition {key} conflicts with whole-message provenance")
        # The accumulators stay written out: each partition has its own nominal
        # type and its own field on the record, so a fifth one is a deliberate
        # edit here rather than an untyped extra riding along in a list.
        content_parts.append(message[PROMPT_GUARD_CONTENT_PARTS_KEY] if PROMPT_GUARD_CONTENT_PARTS_KEY in message else None)
        tool_argument_parts.append(
            message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
        )
        thinking_parts.append(message[PROMPT_GUARD_THINKING_PARTS_KEY] if PROMPT_GUARD_THINKING_PARTS_KEY in message else None)
        wire.append({key: value for key, value in message.items() if key not in PROMPT_GUARD_PROVENANCE_KEYS})
    detached = deepcopy(wire)
    admitted_parts = tuple(content_parts)
    validate_prompt_guard_content_parts(detached, admitted_parts)
    admitted_arguments = tuple(tool_argument_parts)
    validate_prompt_guard_tool_argument_parts(detached, admitted_arguments)
    admitted_thinking = tuple(thinking_parts)
    validate_prompt_guard_thinking_parts(detached, admitted_thinking)
    return DetachedPromptGuardEvidence(detached, frozenset(trusted), admitted_parts, admitted_arguments, admitted_thinking)


def validate_prompt_guard_thinking_parts(
    messages: Sequence[Mapping[str, Any]], partitions: tuple[PromptGuardThinkingParts | None, ...]
) -> None:
    """Bind nominal inspection units to the complete original native field."""
    if type(partitions) is not tuple or len(partitions) != len(messages):
        raise AuditIntegrityError("Provider thinking partition lacks exact message positions")
    for message, entry in zip(messages, partitions, strict=True):
        if entry is None:
            continue
        if (
            type(entry) is not PromptGuardThinkingParts
            or "role" not in message
            or message["role"] != "assistant"
            or "thinking_blocks" not in message
        ):
            raise AuditIntegrityError("Provider thinking partition has no owned assistant field")
        try:
            complete = _parts_cover_chunks(entry.parts, _standard_json_chunks(message["thinking_blocks"]))
        except (TypeError, ValueError):
            raise AuditIntegrityError("Provider thinking field is not finite wire JSON") from None
        if not complete:
            raise AuditIntegrityError("Provider thinking partition does not cover its complete wire field")


def validate_prompt_guard_tool_argument_parts(
    messages: Sequence[Mapping[str, Any]], partitions: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...]
) -> None:
    """Bind each unique argument partition to its complete original wire string."""
    if type(partitions) is not tuple or len(partitions) != len(messages):
        raise AuditIntegrityError("Prompt guard tool argument partition lacks exact message positions")
    for message, entries in zip(messages, partitions, strict=True):
        if entries is None:
            continue
        if type(entries) is not tuple or any(type(entry) is not PromptGuardToolArgumentParts for entry in entries):
            raise AuditIntegrityError("Prompt guard tool argument partition is not an owned producer identity")
        if len({entry.tool_call_index for entry in entries}) != len(entries):
            raise AuditIntegrityError("Prompt guard tool argument partition overlaps a tool call")
        calls = message["tool_calls"] if "tool_calls" in message else None
        if not isinstance(calls, (list, tuple)):
            raise AuditIntegrityError("Prompt guard tool argument partition has no complete wire tool calls")
        for entry in entries:
            if entry.tool_call_index >= len(calls):
                raise AuditIntegrityError("Prompt guard tool argument partition names an absent tool call")
            call = calls[entry.tool_call_index]
            function = call["function"] if isinstance(call, Mapping) and "function" in call else None
            arguments = function["arguments"] if isinstance(function, Mapping) and "arguments" in function else None
            if type(arguments) is not str or not _parts_cover_text(entry.parts, arguments):
                raise AuditIntegrityError("Prompt guard tool argument partition does not cover the complete wire arguments")


def detach_prompt_guard_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], frozenset[int]]:
    """Preserve the existing two-value wire projection while stripping both markers."""
    evidence = detach_prompt_guard_evidence(messages)
    return evidence.messages, evidence.trusted_message_indexes


@dataclass(frozen=True, slots=True)
class PromptGuardRequest:
    """Exact request evidence, with genuine user input separate from documents."""

    role: PromptGuardRole
    user_prompt: str = field(repr=False)
    documents: tuple[str, ...] = field(repr=False)
    request_hash: str

    def __post_init__(self) -> None:
        if self.role not in ("composer", "advisor"):
            raise AuditIntegrityError("Prompt guard target is outside its closed vocabulary")
        if type(self.user_prompt) is not str or type(self.documents) is not tuple or any(type(item) is not str for item in self.documents):
            raise AuditIntegrityError("Prompt guard content lacks immutable text provenance")
        if not is_lower_sha256_hex(self.request_hash):
            raise AuditIntegrityError("Prompt guard request lacks an exact content binding")


@dataclass(frozen=True, slots=True)
class PromptGuardDecision:
    """A specialist service's admitted decision; never arbitrary provider prose."""

    disposition: PromptGuardDisposition
    reason: PromptGuardReason

    def __post_init__(self) -> None:
        pairs = {
            "allow": ("clean",),
            "blocked": ("policy_blocked",),
            "unavailable": ("service_error", "timeout", "invalid_response", "unsupported_input", "guard_not_configured"),
            "cancelled": ("cancelled",),
            "debug_bypass": ("guard_not_configured",),
        }
        if self.disposition not in pairs or self.reason not in pairs[self.disposition]:
            raise AuditIntegrityError("Prompt guard decision is outside its closed coherent vocabulary")


class PromptGuardClient(ABC):
    """Nominal extension point for dedicated detector transports."""

    @property
    @abstractmethod
    def adapter(self) -> str:
        """Nonsecret registered adapter identity."""

    @property
    @abstractmethod
    def policy_fingerprint(self) -> str:
        """Stable identity of the operator's nonsecret service/policy settings."""

    @property
    @abstractmethod
    def timeout_seconds(self) -> float:
        """Bound for a complete invocation, not each individual segment."""

    @abstractmethod
    async def inspect(self, request: PromptGuardRequest) -> PromptGuardDecision:
        """Check every supported input surface or return unavailable."""
