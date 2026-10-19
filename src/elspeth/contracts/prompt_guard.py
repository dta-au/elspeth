"""Owned inputs and closed outcomes for authoring prompt-guard services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex

PromptGuardRole = Literal["composer", "advisor"]
PromptGuardMode = Literal["required", "debug-when-unconfigured"]
PromptGuardDisposition = Literal["allow", "blocked", "unavailable", "cancelled", "debug_bypass"]
PromptGuardReason = Literal[
    "clean", "policy_blocked", "service_error", "timeout", "invalid_response", "unsupported_input", "cancelled", "guard_not_configured"
]

PROMPT_GUARD_TRUST_KEY = "_elspeth_prompt_guard_trust"
PROMPT_GUARD_CONTENT_PARTS_KEY = "_elspeth_prompt_guard_content_parts"
PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY = "_elspeth_prompt_guard_tool_argument_parts"
PROMPT_GUARD_MAX_TEXT_CHARACTERS = 10_000
PROMPT_GUARD_MAX_DOCUMENTS = 128
# One user prompt plus complete packed documents, with at most four UTF-8
# bytes per valid character. Azure's per-call limits are enforced separately.
PROMPT_GUARD_MAX_INPUT_UTF8_BYTES = (PROMPT_GUARD_MAX_DOCUMENTS + 1) * PROMPT_GUARD_MAX_TEXT_CHARACTERS * 4


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


type PromptGuardTextMessage = dict[str, str | PromptGuardTrust | tuple[PromptGuardContentPart, ...]]


@dataclass(frozen=True, slots=True)
class DetachedPromptGuardEvidence:
    """Detached external wire values and separate, immutable producer evidence."""

    messages: list[dict[str, Any]] = field(repr=False)
    trusted_message_indexes: frozenset[int]
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] = field(repr=False)
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] = field(repr=False)


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
        if "content" not in message or type(message["content"]) is not str or "".join(part.text for part in parts) != message["content"]:
            raise AuditIntegrityError("Prompt guard content partition does not cover the complete wire content")


def detach_prompt_guard_evidence(messages: Sequence[Mapping[str, Any]]) -> DetachedPromptGuardEvidence:
    """Separate nominal producer provenance from detached, marker-free wire input.

    Call this before wire hashing, provider-context capture or dispatch. A
    string decoded from user/model JSON cannot mint trusted provenance.
    """
    wire: list[dict[str, Any]] = []
    trusted: set[int] = set()
    content_parts: list[tuple[PromptGuardContentPart, ...] | None] = []
    tool_argument_parts: list[tuple[PromptGuardToolArgumentParts, ...] | None] = []
    for index, message in enumerate(messages):
        if PROMPT_GUARD_TRUST_KEY in message:
            marker = message[PROMPT_GUARD_TRUST_KEY]
            if type(marker) is not PromptGuardTrust:
                raise AuditIntegrityError("Prompt guard trust marker is not an owned producer identity")
            trusted.add(index)
        parts = message[PROMPT_GUARD_CONTENT_PARTS_KEY] if PROMPT_GUARD_CONTENT_PARTS_KEY in message else None
        if PROMPT_GUARD_CONTENT_PARTS_KEY in message and (parts is None or PROMPT_GUARD_TRUST_KEY in message):
            raise AuditIntegrityError("Prompt guard content partition conflicts with whole-message provenance")
        content_parts.append(parts)
        arguments = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message else None
        if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message and (arguments is None or PROMPT_GUARD_TRUST_KEY in message):
            raise AuditIntegrityError("Prompt guard tool argument partition conflicts with whole-message provenance")
        tool_argument_parts.append(arguments)
        wire.append(
            {
                key: value
                for key, value in message.items()
                if key not in (PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY)
            }
        )
    detached = deepcopy(wire)
    admitted_parts = tuple(content_parts)
    validate_prompt_guard_content_parts(detached, admitted_parts)
    admitted_arguments = tuple(tool_argument_parts)
    validate_prompt_guard_tool_argument_parts(detached, admitted_arguments)
    return DetachedPromptGuardEvidence(detached, frozenset(trusted), admitted_parts, admitted_arguments)


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
            if type(arguments) is not str or "".join(part.text for part in entry.parts) != arguments:
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
