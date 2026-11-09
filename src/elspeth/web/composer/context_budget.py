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
    policy_hash: str
    bytes_per_token: float
    request_bytes_before: int
    estimated_tokens_before: int
    request_bytes_after: int
    estimated_tokens_after: int
    shed_keys: tuple[str, ...]


class ContextBudgetRefusal(Exception):
    """Protected content alone exceeds a ceiling."""

    def __init__(self, *, ceiling: Literal["tokens", "bytes"], estimated_tokens: int, request_bytes: int) -> None:
        super().__init__(
            f"context budget exceeded the {ceiling} ceiling: estimated_tokens={estimated_tokens} request_bytes={request_bytes}"
        )
        self.ceiling = ceiling
        self.estimated_tokens = estimated_tokens
        self.request_bytes = request_bytes


def decide_context_budget(
    *,
    policy: ContextBudgetPolicy,
    request_bytes: int,
    segments: Sequence[SheddableSegment],
    bytes_per_token: float | None,
    previously_shed: frozenset[str],
) -> ContextBudgetDecision:
    ratio = bytes_per_token if bytes_per_token is not None and bytes_per_token > 0 else policy.fallback_bytes_per_token

    def tokens(size: int) -> int:
        return math.ceil(size / ratio)

    high = policy.shed_above * policy.max_prompt_tokens
    low = policy.shed_to * policy.max_prompt_tokens
    tokens_before = tokens(request_bytes)

    sticky = [segment for segment in segments if segment.key in previously_shed]
    over = tokens_before > high or request_bytes > policy.max_request_bytes

    shed: list[SheddableSegment]
    if not over:
        shed = sticky if tokens_before > low else []
    else:
        remaining = sorted(
            (segment for segment in segments if segment.key not in previously_shed),
            key=lambda segment: _KIND_RANK[segment.kind],
        )
        shed = []
        after = request_bytes
        for segment in [*sticky, *remaining]:
            if tokens(after) <= low and after <= policy.max_request_bytes:
                break
            shed.append(segment)
            after -= segment.saving_bytes

    bytes_after = request_bytes - sum(segment.saving_bytes for segment in shed)
    tokens_after = tokens(bytes_after)
    if tokens_after > policy.max_prompt_tokens:
        raise ContextBudgetRefusal(ceiling="tokens", estimated_tokens=tokens_after, request_bytes=bytes_after)
    if bytes_after > policy.max_request_bytes:
        raise ContextBudgetRefusal(ceiling="bytes", estimated_tokens=tokens_after, request_bytes=bytes_after)

    return ContextBudgetDecision(
        policy_hash=policy.audit_hash,
        bytes_per_token=ratio,
        request_bytes_before=request_bytes,
        estimated_tokens_before=tokens_before,
        request_bytes_after=bytes_after,
        estimated_tokens_after=tokens_after,
        shed_keys=tuple(segment.key for segment in shed),
    )
