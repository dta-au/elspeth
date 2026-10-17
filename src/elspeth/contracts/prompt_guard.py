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


class PromptGuardTrust(Enum):
    """In-process provenance minted by owned builders, never decoded from JSON."""

    INSTRUCTION = "operator_instruction"
    REFERENCE = "operator_reference"


def detach_prompt_guard_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], frozenset[int]]:
    """Separate nominal producer provenance from detached, marker-free wire input.

    Call this before wire hashing, provider-context capture or dispatch. A
    string decoded from user/model JSON cannot mint trusted provenance.
    """
    wire: list[dict[str, Any]] = []
    trusted: set[int] = set()
    for index, message in enumerate(messages):
        if PROMPT_GUARD_TRUST_KEY in message:
            marker = message[PROMPT_GUARD_TRUST_KEY]
            if type(marker) is not PromptGuardTrust:
                raise AuditIntegrityError("Prompt guard trust marker is not an owned producer identity")
            trusted.add(index)
        wire.append({key: value for key, value in message.items() if key != PROMPT_GUARD_TRUST_KEY})
    return deepcopy(wire), frozenset(trusted)


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
