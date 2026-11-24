"""The provider library's error shapes that Composer retry decisions depend on.

The Composer retries by exception class. Each row sends one failure through
real ``litellm.acompletion`` (mock transport, no network) and pins the arriving
class, whether the Composer retries it, and whether the provider's Retry-After
reached the classifier. A failing row means the provider library's error shape
changed: recheck retry safety before updating the row.

On the OpenAI, Anthropic and Bedrock routes, a failure to read an already
billed response arrives in a class the Composer does not retry. On the
OpenRouter route it does not: every one of these failures arrives as a general
API error, which the Composer already retried before bounded 429 retries, so
the OpenRouter rows pin existing retries of failures that may follow a billed
response. They are not evidence that those retries are safe. A 403 also
arrives as a general API error on the OpenAI and OpenRouter routes, so there
it is retried and reported as unavailable.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.provider_errors import classify_provider_failure
from tests.helpers.litellm_dispatch import FAILURES, ROUTES, Failure, Route, provider_failure

# (arriving class, retried by the main loop and planner, Retry-After seconds seen)
_NOT = (False, None)
_RETRIED = (True, None)
_EXPECTED: dict[tuple[Route, Failure], tuple[str, bool, float | None]] = {
    ("openai", "429_retry_after"): ("RateLimitError", *_RETRIED),
    ("openai", "429_quota"): ("RateLimitError", *_RETRIED),
    ("openai", "403"): ("APIError", *_RETRIED),
    ("openai", "500"): ("InternalServerError", *_NOT),
    ("openai", "503"): ("ServiceUnavailableError", *_RETRIED),
    ("openai", "connect"): ("InternalServerError", *_NOT),
    ("openai", "read"): ("InternalServerError", *_NOT),
    ("openai", "malformed_200"): ("InternalServerError", *_NOT),
    ("anthropic", "429_retry_after"): ("RateLimitError", *_RETRIED),
    ("anthropic", "429_quota"): ("RateLimitError", *_RETRIED),
    ("anthropic", "403"): ("PermissionDeniedError", *_NOT),
    ("anthropic", "500"): ("InternalServerError", *_NOT),
    ("anthropic", "503"): ("ServiceUnavailableError", *_RETRIED),
    ("anthropic", "connect"): ("InternalServerError", *_NOT),
    ("anthropic", "read"): ("InternalServerError", *_NOT),
    ("anthropic", "malformed_200"): ("APIConnectionError", *_NOT),
    # Existing retries of failures that may follow a billed response (see the
    # module docstring); pinned so a change in either direction surfaces.
    ("openrouter", "429_retry_after"): ("RateLimitError", *_RETRIED),
    ("openrouter", "429_quota"): ("RateLimitError", *_RETRIED),
    ("openrouter", "403"): ("APIError", *_RETRIED),
    ("openrouter", "500"): ("APIError", *_RETRIED),
    ("openrouter", "503"): ("ServiceUnavailableError", *_RETRIED),
    ("openrouter", "connect"): ("APIError", *_RETRIED),
    ("openrouter", "read"): ("APIError", *_RETRIED),
    ("openrouter", "malformed_200"): ("APIError", *_RETRIED),
    ("bedrock", "429_retry_after"): ("RateLimitError", True, 7.0),
    ("bedrock", "429_quota"): ("RateLimitError", *_RETRIED),
    ("bedrock", "403"): ("PermissionDeniedError", *_NOT),
    ("bedrock", "500"): ("ServiceUnavailableError", *_RETRIED),
    ("bedrock", "503"): ("ServiceUnavailableError", *_RETRIED),
    ("bedrock", "connect"): ("APIConnectionError", *_NOT),
    ("bedrock", "read"): ("APIConnectionError", *_NOT),
    ("bedrock", "malformed_200"): ("BadRequestError", *_NOT),
}


def test_every_route_and_failure_has_an_expected_shape() -> None:
    assert set(_EXPECTED) == {(route, failure) for route in ROUTES for failure in FAILURES}


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "failure"), [pytest.param(route, failure, id=f"{route}-{failure}") for route, failure in _EXPECTED])
async def test_provider_library_error_shape_still_supports_retry_safety(route: Route, failure: Failure) -> None:
    raised = await provider_failure(route, failure)
    classified = classify_provider_failure(raised)
    assert classified is not None
    observed = (
        type(raised).__name__,
        classified.retryable,
        None if classified.retry_after is None else classified.retry_after.seconds,
    )
    assert observed == _EXPECTED[(route, failure)], (
        f"the provider library's error shape changed for {route}/{failure}; recheck retry safety: observed {observed}"
    )
    # Only a real provider 429 is rate limited; the review path never re-sends one.
    assert classified.rate_limited is failure.startswith("429")
