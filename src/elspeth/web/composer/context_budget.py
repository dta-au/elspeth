"""Pure context-budget decision for composer provider requests.

Given the size of a request and the segments that may be shed, decide which
segments to shed so the request fits the token and byte ceilings, or refuse.
The module performs no I/O and knows nothing about message shapes; callers
translate messages into :class:`SheddableSegment` and apply the decision.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from elspeth.contracts.composer_llm_audit import ContextBudgetAnchorStatus
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY, PromptGuardTrust, detach_prompt_guard_evidence
from elspeth.core.canonical import canonical_json, stable_hash

CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN: Final[float] = 3.0
# Every size in this module is WIRE bytes: the canonical JSON of the request
# body's ``{messages, tools}`` as sent, so content, tool-call arguments,
# replayed thinking blocks and tool definitions all count. The ratio is wire
# bytes per provider-reported prompt token.
#
# Historical ratios remain diagnostic only. A same-exchange provider count
# can price unchanged protected messages; without one, only authored bytes get the fallback rate.
# Release always uses the byte yardstick, independently of anchor availability.
CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN: Final[float] = 2.0
CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN: Final[float] = 6.0
CONTEXT_BUDGET_TOOL_RESULT_STUB: Final[str] = "[Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]"


def escaped_bytes(text: str) -> int:
    """UTF-8 wire bytes ``text`` occupies inside a JSON string of the canonical request, without the quotes.

    String escaping is per character, so a substring's share of an escaped
    string is its own escaped length. Callers size text nested in a message's
    content (a serialized payload or transcript) with it.
    """
    return len(canonical_json(text).encode("utf-8")) - 2


def protected_reference_bytes(messages: Sequence[Mapping[str, object]], *, excluded_positions: frozenset[int]) -> int:
    """Count only producer-labelled reference content in protected messages."""
    evidence = detach_prompt_guard_evidence(messages)
    total = 0
    for index, message in enumerate(messages):
        if index in excluded_positions:
            continue
        if message.get(PROMPT_GUARD_TRUST_KEY) is PromptGuardTrust.REFERENCE:
            content = message["content"]
            if type(content) is not str:
                raise ValueError("producer-labelled reference content must be text")
            total += escaped_bytes(content)
        elif (parts := evidence.content_parts[index]) is not None:
            total += sum(escaped_bytes(part.text) for part in parts if part.trust is PromptGuardTrust.REFERENCE)
    return total


class SheddableKind(StrEnum):
    """Kinds of sheddable segment; definition order IS the shed order."""

    TOOL_RESULT = "tool_result"
    ASSISTANT_PROSE = "assistant_prose"
    HATCH_EVENT = "hatch_event"
    USER_REQUEST = "user_request"


_KIND_RANK: Final[dict[SheddableKind, int]] = {kind: rank for rank, kind in enumerate(SheddableKind)}


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    max_prompt_tokens: int
    max_request_bytes: int
    shed_above: float
    shed_to: float
    fallback_bytes_per_token: float = CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN

    def __post_init__(self) -> None:
        for name, value in (
            ("max_prompt_tokens", self.max_prompt_tokens),
            ("max_request_bytes", self.max_request_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        for name, fvalue in (
            ("shed_above", self.shed_above),
            ("shed_to", self.shed_to),
            ("fallback_bytes_per_token", self.fallback_bytes_per_token),
        ):
            if type(fvalue) is not float or not math.isfinite(fvalue):
                raise ValueError(f"{name} must be a finite float")
        if self.fallback_bytes_per_token <= 0:
            raise ValueError("fallback_bytes_per_token must be positive")
        if not (0 < self.shed_to < self.shed_above <= 1):
            raise ValueError("shed marks must satisfy 0 < shed_to < shed_above <= 1")

    @property
    def audit_hash(self) -> str:
        return stable_hash(
            {
                "schema": "composer.context-budget.v2",
                "max_prompt_tokens": self.max_prompt_tokens,
                "max_request_bytes": self.max_request_bytes,
                "shed_above": repr(self.shed_above),
                "shed_to": repr(self.shed_to),
                "fallback_bytes_per_token": repr(self.fallback_bytes_per_token),
            }
        )


@dataclass(frozen=True, slots=True)
class SheddableSegment:
    kind: SheddableKind
    key: str
    saving_bytes: int

    def __post_init__(self) -> None:
        if type(self.saving_bytes) is not int or self.saving_bytes <= 0:
            raise ValueError("saving_bytes must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class ContextBudgetPricing:
    """Content-free inputs and the selected pricing arm, including refusals."""

    mode: Literal["anchored", "conservative", "authored_prefix"]
    anchor_status: ContextBudgetAnchorStatus
    anchor_tokens: int | None
    anchor_wire_bytes: int
    shared_bytes: int
    request_bytes: int
    authored_bytes: int


@dataclass(frozen=True, slots=True)
class ContextBudgetDecision:
    """What to shed, with the request's wire size before and after.

    ``request_bytes_after`` is an upper bound on the wire size of the request
    once the decision is applied (see :func:`decide_context_budget`).
    """

    policy_hash: str
    bytes_per_token: float
    request_bytes_before: int
    estimated_tokens_before: int
    request_bytes_after: int
    estimated_tokens_after: int
    shed_keys: tuple[str, ...]
    pricing: ContextBudgetPricing


class ContextBudgetRefusal(Exception):
    """Protected content alone exceeds a ceiling; ``request_bytes`` is wire bytes."""

    def __init__(
        self,
        *,
        ceiling: Literal["tokens", "bytes"],
        estimated_tokens: int,
        request_bytes: int,
        pricing: ContextBudgetPricing | None = None,
    ) -> None:
        super().__init__(
            f"context budget exceeded the {ceiling} ceiling: estimated_tokens={estimated_tokens} request_bytes={request_bytes}"
        )
        self.ceiling = ceiling
        self.estimated_tokens = estimated_tokens
        self.request_bytes = request_bytes
        self.pricing = pricing


def decide_context_budget(
    *,
    policy: ContextBudgetPolicy,
    request_bytes: int,
    segments: Sequence[SheddableSegment],
    bytes_per_token: float | None,
    previously_shed: frozenset[str],
    held: frozenset[str] = frozenset(),
    anchor_tokens: int | None = None,
    anchor_shared_bytes: int = 0,
    anchor_wire_bytes: int = 0,
    anchor_status: ContextBudgetAnchorStatus = "no_previous_call",
    authored_bytes: int = 0,
) -> ContextBudgetDecision:
    """Decide which segments to shed from one request, or refuse.

    ``previously_shed`` are keys an earlier call shed; they follow the release
    rule below. ``held`` are keys that must stay shed whatever the size: a
    caller passes the tool results stubbed earlier in the current turn, which
    stay stubbed for the rest of that turn.

    ``request_bytes`` is the wire size of the request as it will be sent: the
    canonical JSON of the body's ``{messages, tools}``, escaping and framing
    included. One measure serves both ceilings. It is what the gateway's body
    cap sees, so ``max_request_bytes`` bounds it directly; and it counts
    everything the model reads (content, tool-call arguments, thinking
    blocks, tool definitions). Authored bytes keep the fallback rate even when
    an anchor exists. The other arm pays the anchor's ENTIRE reported count
    plus every non-shared byte. Use the smaller of these two upper bounds.
    Shared bytes must belong only to unchanged protected messages/tools;
    no segment saving may overlap them. This deliberately gives no token
    credit for content removed from the anchor. It assumes provider accounting
    is additive across independently framed messages/tools and unchanged
    content on the same model/dialect retains its price. Hidden provider-side
    expansion is outside the byte bound, as it is for conservative admission.

    A segment's ``saving_bytes`` must never exceed what shedding it removes
    from the wire, and should be close to it: callers count what the segment
    occupies on the wire, escapes and framing included. Because savings are
    lower bounds, the sizes computed after shedding over-state what will be
    sent and an admitted request fits both ceilings. A looser bound (content
    bytes alone) would also be safe but leaves the escapes of everything shed
    in the size, which grows with the history until a request that fits once
    shed is refused.
    """
    if bytes_per_token is not None and not (math.isfinite(bytes_per_token) and bytes_per_token > 0):
        raise ValueError("bytes_per_token must be a finite positive number or None")
    if type(request_bytes) is not int or request_bytes < 0:
        raise ValueError("request_bytes must be a non-negative exact integer")
    if len({segment.key for segment in segments}) != len(segments):
        raise ValueError("segment keys must be unique")
    savings = sum(segment.saving_bytes for segment in segments)
    if savings > request_bytes:
        raise ValueError("segment savings exceed request_bytes")
    if type(anchor_shared_bytes) is not int or not 0 <= anchor_shared_bytes <= request_bytes - savings:
        raise ValueError("anchor shared bytes must exclude every sheddable byte")
    if anchor_tokens is not None and (type(anchor_tokens) is not int or anchor_tokens <= 0):
        raise ValueError("anchor_tokens must be a positive exact integer")
    if anchor_tokens is None and anchor_shared_bytes:
        raise ValueError("shared bytes require an anchor")
    if type(authored_bytes) is not int or not 0 <= authored_bytes <= request_bytes - savings:
        raise ValueError("authored bytes must exclude every sheddable byte")
    # Legacy ratio input is accepted and validated for API compatibility only.
    # It cannot classify foreign bytes or change admission/release decisions.
    ratio = 1.0
    high = policy.shed_above * policy.max_prompt_tokens
    low = policy.shed_to * policy.max_prompt_tokens

    # When the protected prefix makes the low mark unreachable, shed only
    # to the high mark. If even that is unreachable, exhaust the segments
    # before judging the hard ceilings. Current requests are never segments.
    def authored_tokens(size: int) -> int:
        return size - authored_bytes + math.ceil(authored_bytes / policy.fallback_bytes_per_token)

    def tokens(size: int) -> int:
        if anchor_tokens is None:
            return authored_tokens(size)
        return min(authored_tokens(size), anchor_tokens + size - anchor_shared_bytes)

    def pricing(size: int) -> ContextBudgetPricing:
        mode: Literal["anchored", "conservative", "authored_prefix"] = "conservative"
        if anchor_tokens is not None and anchor_tokens + size - anchor_shared_bytes < authored_tokens(size):
            mode = "anchored"
        elif authored_bytes:
            mode = "authored_prefix"
        return ContextBudgetPricing(mode, anchor_status, anchor_tokens, anchor_wire_bytes, anchor_shared_bytes, size, authored_bytes)

    target = high if tokens(request_bytes - savings) > low else low

    def fits_low(size: int) -> bool:
        return size <= low and size <= policy.max_request_bytes

    tokens_before = tokens(request_bytes)

    # Hysteresis. Segments shed on an earlier call are restored only when the
    # fully unshed request is back at or below the low mark (and under the byte
    # ceiling). Otherwise they stay shed, and whether MORE must go is judged on
    # the request with that sticky set already shed -- what would be sent if
    # nothing new were shed. Judging it on the unshed size would shed again on
    # every call once a session had crossed the high mark, pinning the request
    # at the low mark and changing the sent prefix each time. Held keys are
    # never restored.
    released = fits_low(request_bytes)
    sticky = [segment for segment in segments if segment.key in held or (segment.key in previously_shed and not released)]
    kept = request_bytes - sum(segment.saving_bytes for segment in sticky)
    over = tokens(kept) > high or kept > policy.max_request_bytes

    shed: list[SheddableSegment] = list(sticky)
    if over:
        remaining = sorted(
            (segment for segment in segments if segment.key not in previously_shed and segment.key not in held),
            key=lambda segment: _KIND_RANK[segment.kind],
        )
        after = kept
        for segment in remaining:
            if tokens(after) <= target and after <= policy.max_request_bytes:
                break
            shed.append(segment)
            after -= segment.saving_bytes

    bytes_after = request_bytes - sum(segment.saving_bytes for segment in shed)
    tokens_after = tokens(bytes_after)
    if tokens_after > policy.max_prompt_tokens:
        raise ContextBudgetRefusal(ceiling="tokens", estimated_tokens=tokens_after, request_bytes=bytes_after, pricing=pricing(bytes_after))
    if bytes_after > policy.max_request_bytes:
        raise ContextBudgetRefusal(ceiling="bytes", estimated_tokens=tokens_after, request_bytes=bytes_after, pricing=pricing(bytes_after))

    return ContextBudgetDecision(
        policy_hash=policy.audit_hash,
        bytes_per_token=ratio,
        request_bytes_before=request_bytes,
        estimated_tokens_before=tokens_before,
        request_bytes_after=bytes_after,
        estimated_tokens_after=tokens_after,
        shed_keys=tuple(segment.key for segment in shed),
        pricing=pricing(bytes_after),
    )
