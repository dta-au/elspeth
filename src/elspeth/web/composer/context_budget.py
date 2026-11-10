"""Pure context-budget decision for composer provider requests.

Given the size of a request and the segments that may be shed, decide which
segments to shed so the request fits the token and byte ceilings, or refuse.
The module performs no I/O and knows nothing about message shapes; callers
translate messages into :class:`SheddableSegment` and apply the decision.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from elspeth.core.canonical import stable_hash

CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN: Final[float] = 3.0
CONTEXT_BUDGET_TOOL_RESULT_STUB: Final[str] = "[Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]"


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
                "schema": "composer.context-budget.v1",
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
class ContextBudgetDecision:
    """What to shed, with both sizes before and after.

    ``wire_bytes_after`` is an upper bound on the wire size of the request
    once the decision is applied (see :func:`decide_context_budget`).
    """

    policy_hash: str
    bytes_per_token: float
    content_bytes_before: int
    wire_bytes_before: int
    estimated_tokens_before: int
    content_bytes_after: int
    wire_bytes_after: int
    estimated_tokens_after: int
    shed_keys: tuple[str, ...]


class ContextBudgetRefusal(Exception):
    """Protected content alone exceeds a ceiling.

    ``ceiling`` names the one that was hit: ``tokens`` is ``estimated_tokens``
    (from ``content_bytes``) against the prompt-token ceiling; ``bytes`` is
    ``wire_bytes`` against the request-byte ceiling.
    """

    def __init__(self, *, ceiling: Literal["tokens", "bytes"], estimated_tokens: int, content_bytes: int, wire_bytes: int) -> None:
        super().__init__(
            f"context budget exceeded the {ceiling} ceiling: "
            f"estimated_tokens={estimated_tokens} content_bytes={content_bytes} wire_bytes={wire_bytes}"
        )
        self.ceiling = ceiling
        self.estimated_tokens = estimated_tokens
        self.content_bytes = content_bytes
        self.wire_bytes = wire_bytes


def decide_context_budget(
    *,
    policy: ContextBudgetPolicy,
    content_bytes: int,
    wire_bytes: int,
    segments: Sequence[SheddableSegment],
    bytes_per_token: float | None,
    previously_shed: frozenset[str],
) -> ContextBudgetDecision:
    """Decide which segments to shed from one request, or refuse.

    The request is given in two sizes, both of the request as it will be sent:

    - ``content_bytes``: the content split's total (a string message counts
      its content's UTF-8 bytes). The stored bytes-per-token ratio is
      calibrated on this measure, so it gives the token estimate and is what
      the shed marks and the token ceiling are judged on.
    - ``wire_bytes``: the canonical JSON of the request body's
      ``{messages, tools}``, escaping and framing included. It is what the
      gateway's body cap sees, so it is what ``max_request_bytes`` bounds.

    A segment's ``saving_bytes`` is content bytes: exact for the token
    estimate. Shedding a segment never saves less on the wire than in content
    (JSON escaping and message framing only add bytes, and the constant stub
    escapes to itself), so subtracting content savings from ``wire_bytes``
    over-states the wire size that remains. An admitted request therefore fits
    the byte ceiling; the cost is that shedding may go one segment further
    than the wire strictly needed.
    """
    ratio = bytes_per_token if bytes_per_token is not None and bytes_per_token > 0 else policy.fallback_bytes_per_token

    def tokens(size: int) -> int:
        return math.ceil(size / ratio)

    def fits_low(content: int, wire: int) -> bool:
        return tokens(content) <= low and wire <= policy.max_request_bytes

    high = policy.shed_above * policy.max_prompt_tokens
    low = policy.shed_to * policy.max_prompt_tokens
    tokens_before = tokens(content_bytes)

    # Hysteresis. Segments shed on an earlier call are restored only when the
    # fully unshed request is back at or below the low mark (and under the byte
    # ceiling). Otherwise they stay shed, and whether MORE must go is judged on
    # the request with that sticky set already shed -- what would be sent if
    # nothing new were shed. Judging it on the unshed size would shed again on
    # every call once a session had crossed the high mark, pinning the request
    # at the low mark and changing the sent prefix each time.
    sticky: list[SheddableSegment] = []
    if not fits_low(content_bytes, wire_bytes):
        sticky = [segment for segment in segments if segment.key in previously_shed]
    sticky_saving = sum(segment.saving_bytes for segment in sticky)
    kept_content = content_bytes - sticky_saving
    kept_wire = wire_bytes - sticky_saving
    over = tokens(kept_content) > high or kept_wire > policy.max_request_bytes

    shed: list[SheddableSegment] = list(sticky)
    if over:
        remaining = sorted(
            (segment for segment in segments if segment.key not in previously_shed),
            key=lambda segment: _KIND_RANK[segment.kind],
        )
        after_content, after_wire = kept_content, kept_wire
        for segment in remaining:
            if fits_low(after_content, after_wire):
                break
            shed.append(segment)
            after_content -= segment.saving_bytes
            after_wire -= segment.saving_bytes

    saved = sum(segment.saving_bytes for segment in shed)
    content_after = content_bytes - saved
    wire_after = wire_bytes - saved
    tokens_after = tokens(content_after)
    if tokens_after > policy.max_prompt_tokens:
        raise ContextBudgetRefusal(ceiling="tokens", estimated_tokens=tokens_after, content_bytes=content_after, wire_bytes=wire_after)
    if wire_after > policy.max_request_bytes:
        raise ContextBudgetRefusal(ceiling="bytes", estimated_tokens=tokens_after, content_bytes=content_after, wire_bytes=wire_after)

    return ContextBudgetDecision(
        policy_hash=policy.audit_hash,
        bytes_per_token=ratio,
        content_bytes_before=content_bytes,
        wire_bytes_before=wire_bytes,
        estimated_tokens_before=tokens_before,
        content_bytes_after=content_after,
        wire_bytes_after=wire_after,
        estimated_tokens_after=tokens_after,
        shed_keys=tuple(segment.key for segment in shed),
    )
