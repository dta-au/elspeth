"""Pool configuration for concurrent API transforms."""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from elspeth.plugins.infrastructure.pooling.throttle import ThrottleConfig


def validate_pool_delay_invariants(
    *,
    min_dispatch_delay_ms: int,
    max_dispatch_delay_ms: int,
    recovery_step_ms: int,
    pooled: bool,
) -> None:
    """Validate delay ordering and, for a pooled config, AIMD backoff viability.

    Shared by ``PoolConfig`` and by configs that carry the pool knobs flat
    (``LLMConfig``), so the rule has one authority and a flat config can run
    it inside its own ``model_validate``. Ensures that:

    1. min_dispatch_delay_ms <= max_dispatch_delay_ms (always: the pair is
       contradictory whether or not a pool is built)
    2. when ``pooled``, AIMD backoff can actually produce a non-zero delay,
       preventing infinite-speed retry loops on capacity errors

    Raises:
        ValueError: naming the violated invariant.
    """
    if min_dispatch_delay_ms > max_dispatch_delay_ms:
        raise ValueError(f"min_dispatch_delay_ms ({min_dispatch_delay_ms}) cannot exceed max_dispatch_delay_ms ({max_dispatch_delay_ms})")
    if not pooled:
        return
    if max_dispatch_delay_ms == 0:
        raise ValueError("max_dispatch_delay_ms must be > 0 to allow AIMD backoff on capacity errors")
    if min_dispatch_delay_ms == 0 and recovery_step_ms == 0:
        raise ValueError(
            "min_dispatch_delay_ms and recovery_step_ms cannot both be 0: "
            "AIMD recovery would never increase delay above 0, disabling backoff entirely"
        )


class PoolConfig(BaseModel):
    """Pool configuration for concurrent API requests.

    Attributes:
        pool_size: Number of concurrent requests (must be >= 1)
        min_dispatch_delay_ms: Floor for delay between dispatches
        max_dispatch_delay_ms: Ceiling for delay
        backoff_multiplier: Multiply delay on capacity error (must be > 1)
        recovery_step_ms: Subtract from delay on success
        max_capacity_retry_seconds: Max time to retry capacity errors per row
    """

    model_config = {"extra": "forbid"}

    pool_size: int = Field(1, ge=1, description="Number of concurrent requests")
    min_dispatch_delay_ms: int = Field(0, ge=0, description="Minimum dispatch delay in milliseconds")
    max_dispatch_delay_ms: int = Field(5000, ge=0, description="Maximum dispatch delay in milliseconds")
    backoff_multiplier: float = Field(2.0, gt=1.0, description="Backoff multiplier on capacity error")
    recovery_step_ms: int = Field(50, ge=0, description="Recovery step in milliseconds")
    max_capacity_retry_seconds: int = Field(3600, gt=0, description="Max seconds to retry capacity errors")

    @field_validator("backoff_multiplier")
    @classmethod
    def _validate_backoff_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"backoff_multiplier must be finite, got {v}")
        return v

    @model_validator(mode="after")
    def _validate_delay_invariants(self) -> Self:
        """Validate delay ordering and AIMD backoff viability (see ``validate_pool_delay_invariants``)."""
        validate_pool_delay_invariants(
            min_dispatch_delay_ms=self.min_dispatch_delay_ms,
            max_dispatch_delay_ms=self.max_dispatch_delay_ms,
            recovery_step_ms=self.recovery_step_ms,
            pooled=True,
        )
        return self

    def to_throttle_config(self) -> ThrottleConfig:
        """Convert to ThrottleConfig for runtime use."""
        return ThrottleConfig(
            min_dispatch_delay_ms=self.min_dispatch_delay_ms,
            max_dispatch_delay_ms=self.max_dispatch_delay_ms,
            backoff_multiplier=self.backoff_multiplier,
            recovery_step_ms=self.recovery_step_ms,
        )
