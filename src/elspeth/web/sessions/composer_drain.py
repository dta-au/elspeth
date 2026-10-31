"""A shared monotonic drain deadline; no disposition or cleanup authority.

The future HTTP signal hook, worker drain and Stop join must retain this same
absolute deadline. Constructing it or reaching it does not release a lease,
settle an effect or prove that an owned task has finished (runtime design §3).
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

# Runtime design §3.5: two sequential disposition transactions budgeted at
# one second each, plus one second between stage cancellation and disposition.
# This process drain margin is distinct from the retained action reserve.
COMPOSER_DRAIN_DISPOSITION_SECONDS = 2.0
COMPOSER_DRAIN_CANCELLATION_MARGIN_SECONDS = 1.0
COMPOSER_DRAIN_FINISH_RESERVE_SECONDS = COMPOSER_DRAIN_DISPOSITION_SECONDS + COMPOSER_DRAIN_CANCELLATION_MARGIN_SECONDS


def _require_finite_time(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("Composer drain times must be finite numbers")


@dataclass(frozen=True, slots=True)
class ComposerDrainDeadline:
    expires_at: float

    def __post_init__(self) -> None:
        _require_finite_time(self.expires_at)

    @classmethod
    def from_start(cls, *, started_at: float, drain_seconds: float) -> Self:
        _require_finite_time(started_at)
        _require_finite_time(drain_seconds)
        if drain_seconds <= 0:
            raise ValueError("Composer drain duration must be positive")
        return cls(expires_at=started_at + drain_seconds)

    def remaining_seconds(self, *, now: float) -> float:
        _require_finite_time(now)
        remaining = self.expires_at - now
        _require_finite_time(remaining)
        return max(0.0, remaining)


class ComposerDrainSignal:
    """One process signal latch shared by HTTP, membership and the worker."""

    def __init__(self) -> None:
        self.draining = threading.Event()
        self.started_at: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._begin: Callable[[float], ComposerDrainDeadline | None] | None = None

    def bind(self, loop: asyncio.AbstractEventLoop, begin: Callable[[float], ComposerDrainDeadline | None]) -> None:
        if self._loop is not None or self._begin is not None:
            raise RuntimeError("Composer drain signal cannot replace its process worker")
        self._loop, self._begin = loop, begin
        if self.started_at is not None:
            begin(self.started_at)

    def request(self) -> None:
        if self.started_at is not None:
            return
        self.started_at = time.monotonic()
        self.draining.set()
        if self._loop is not None and self._begin is not None:
            self._loop.call_soon_threadsafe(self._begin, self.started_at)
