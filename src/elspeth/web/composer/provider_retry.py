"""One bounded delay policy for Composer provider retries."""

from __future__ import annotations

from elspeth.web.composer.provider_config import LLM_API_RETRY_CALL_MARGIN_SECONDS, LLM_API_RETRY_DELAY_CEILING_SECONDS
from elspeth.web.composer.provider_errors import RetryAfter


def retry_delay_seconds(
    *, attempt: int, retry_after: RetryAfter | None, deadline: float, now: float, base_delay_seconds: float
) -> float | None:
    """Return a safe delay, or refuse retry when the whole delay cannot fit."""
    backoff = base_delay_seconds * 2 ** (attempt - 1)
    delay = max(backoff, retry_after.seconds if retry_after is not None else 0.0)
    cap = min(deadline - now - LLM_API_RETRY_CALL_MARGIN_SECONDS, LLM_API_RETRY_DELAY_CEILING_SECONDS)
    return delay if delay <= cap else None
