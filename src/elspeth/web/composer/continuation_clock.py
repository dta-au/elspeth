"""Independent action allowances and immutable pre-input segment observations."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationSegmentPolicy


class SegmentFit(Enum):
    ADMIT = "admit"
    YIELD = "yield"


def size_segment(action: ContinuationActionPolicy, policy: ContinuationSegmentPolicy) -> float:
    """Size a fresh segment for the complete next action and settlement."""
    _required, size = _action_headroom(action, policy)
    return size


def _finite_seconds(value: object, label: str) -> float:
    if type(value) is not int and type(value) is not float:
        raise ValueError(f"{label} must be a finite number")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"{label} must be a finite number")
    return seconds


def _action_headroom(action: ContinuationActionPolicy, policy: ContinuationSegmentPolicy) -> tuple[float, float]:
    if type(action) is not ContinuationActionPolicy or type(policy) is not ContinuationSegmentPolicy:
        raise TypeError("action sizing requires the actual owned policy types")
    required = _finite_seconds(action.total + policy.reserve, "action and settlement headroom")
    sized = _finite_seconds(required + policy.watermark, "complete segment sizing allowance")
    return required, max(policy.nominal, sized)


def admit_action(
    action: ContinuationActionPolicy,
    policy: ContinuationSegmentPolicy,
    *,
    hard_remaining: float,
    cooperative_remaining: float,
    fresh: bool,
) -> SegmentFit:
    """Admit a new action only while its complete allowance still fits."""
    if type(fresh) is not bool:
        raise ValueError("fresh must be an actual boolean")
    hard = _finite_seconds(hard_remaining, "hard remaining observation")
    soft = _finite_seconds(cooperative_remaining, "cooperative remaining observation")
    if soft > hard:
        raise ValueError("segment observations must have ordered cooperative and hard remaining time")
    required, _size = _action_headroom(action, policy)
    if hard < required:
        if fresh:
            raise ValueError("fresh segment cannot fit its retained next action")
        return SegmentFit.YIELD
    if soft <= 0 or hard <= policy.watermark:
        return SegmentFit.YIELD
    return SegmentFit.ADMIT


@dataclass(frozen=True, slots=True)
class ContinuationTimingObservation:
    """A database-time observation, never a reusable monotonic anchor."""

    sequence: int
    generation: int
    observed_at: datetime
    hard_remaining_seconds: float
    cooperative_remaining_seconds: float
    watermark_seconds: float
    yield_pending: bool

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1 or type(self.generation) is not int or self.generation < 1:
            raise ValueError("timing requires positive integer owner identities")
        _aware_utc(self.observed_at)
        hard = _finite_seconds(self.hard_remaining_seconds, "hard remaining observation")
        soft = _finite_seconds(self.cooperative_remaining_seconds, "cooperative remaining observation")
        watermark = _finite_seconds(self.watermark_seconds, "watermark")
        if hard < 0 or soft < 0 or soft > hard:
            raise ValueError("timing requires ordered nonnegative remaining observations")
        if watermark <= 0:
            raise ValueError("watermark must be positive and finite")
        if type(self.yield_pending) is not bool or self.yield_pending != (soft <= 0 or hard <= watermark):
            raise ValueError("yield pending differs from the actual timing observation")


def _aware_utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError("timing requires aware database times")
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("aware database time cannot be represented in UTC") from exc


def freeze_observation(
    *,
    sequence: int,
    generation: int,
    now: datetime,
    deadline: datetime,
    cooperative: datetime,
    watermark: float,
) -> ContinuationTimingObservation:
    """Freeze elapsed seconds from fixed database deadlines at one instant."""
    observed_at, hard_at, cooperative_at = _aware_utc(now), _aware_utc(deadline), _aware_utc(cooperative)
    mark = _finite_seconds(watermark, "watermark")
    if mark <= 0:
        raise ValueError("watermark must be positive and finite")
    if cooperative_at >= hard_at:
        raise ValueError("cooperative deadline must precede the hard deadline")
    hard = max(0.0, (hard_at - observed_at).total_seconds())
    soft = max(0.0, (cooperative_at - observed_at).total_seconds())
    return ContinuationTimingObservation(sequence, generation, observed_at, hard, soft, mark, soft <= 0 or hard <= mark)


type ContinuationTimedStage = Literal["guard", "quota", "physical", "retention"]


class ContinuationActionClock:
    def __init__(self, policy: ContinuationActionPolicy) -> None:
        self.policy = policy
        self.started = asyncio.get_running_loop().time()
        self.deadline = self.started + policy.total
        self._seen: set[str] = set()

    def finish_preparation(self) -> None:
        if self._seen:
            raise ValueError("action preparation cannot renew or follow another stage")
        if asyncio.get_running_loop().time() > self.started + self.policy.preparation:
            raise TimeoutError("continuation preparation allowance expired")
        self._seen.add("preparation")

    async def run[T](self, stage: ContinuationTimedStage, operation: Callable[[], Awaitable[T]]) -> T:
        if stage in self._seen or "preparation" not in self._seen:
            raise ValueError("continuation action stage repeated or preparation unbound")
        match stage:
            case "guard":
                seconds = self.policy.guard
            case "quota":
                seconds = self.policy.quota
            case "physical":
                seconds = self.policy.physical
            case "retention":
                seconds = self.policy.retention
        if seconds is None:
            raise ValueError("an inapplicable action stage cannot execute")
        self._seen.add(stage)
        deadline = min(self.deadline, asyncio.get_running_loop().time() + seconds)
        async with asyncio.timeout_at(deadline) as timeout:
            result = await operation()
            if timeout.expired() or asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("continuation stage expired while its actual owner was joined")
            return result
