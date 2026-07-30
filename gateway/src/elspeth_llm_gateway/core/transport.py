"""The gateway core's only path to the upstream network.

``UpstreamClient`` turns an adapter's ``InvokePlan`` into exactly one upstream
HTTP call -- or, on a single ``401``, exactly two. It is deliberately
paranoid about the plan it is handed, because adapters are third-party and
untrusted:

- The request URL is built by joining the operator-configured, already
  validated ``upstream_origin`` with ``plan.path`` and asserting the join
  still starts with ``origin + "/"``. Given ``InvokePlan``'s own
  construction-time path validation (no leading slash, no scheme, no
  ``..``), this can never actually fail through the public API -- it is
  defense-in-depth against a future change to how the URL gets assembled,
  not a reachable branch today.
- ``plan.headers`` is re-checked against the same forbidden-name /
  value-length rules ``InvokePlan`` enforces at construction, immediately
  before every send. ``InvokePlan`` is a frozen pydantic model, but its
  ``headers`` field is a plain ``dict`` -- frozen only blocks reassigning
  the field, not mutating the dict object in place. A caller (or another
  concurrent holder of the same plan) mutating ``plan.headers`` after
  construction is exactly the case this second check exists to catch,
  before any network call is made.
- Redirects are never followed (``follow_redirects=False``); any ``3xx``
  is treated as an invalid upstream response rather than silently chased.
- Exactly one replay is permitted on ``401``: the cached token is
  invalidated, a fresh one is fetched, and the request is retried once. A
  second ``401`` is reported as ``upstream_unauthorized`` without a further
  replay.
- ``429`` is core-owned (``upstream_rate_limited``) and never reaches an
  adapter for classification; every other status (2xx, 4xx other than 401
  and 429, 5xx) is returned as an ``UpstreamResult`` for the adapter to
  classify.
- ``httpx.TimeoutException`` is checked *before* ``httpx.TransportError``:
  the former is a subclass of the latter, so checking them in the wrong
  order would silently reclassify every timeout as
  ``upstream_unavailable``.
- Response bodies are read via a bounded streaming loop -- never
  ``.content`` / ``.aread()`` on an unbounded body -- so an oversized
  response is rejected once at most ``max_response_bytes + 1`` bytes have
  been pulled from the wire, not after the whole body has been buffered.
"""

from dataclasses import dataclass

import httpx

from elspeth_llm_gateway.core.config import GatewayConfig
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.oauth import TokenManager
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json
from elspeth_llm_gateway.sdk.protocol import InvokePlan

# Mirrors InvokePlan's own construction-time header rule (sdk/protocol.py):
# duplicated here, not imported, because the check that matters is the one
# run again at send time against whatever plan.headers looks like *now* --
# after any post-construction mutation of the (mutable) dict.
_FORBIDDEN_HEADER_NAMES = frozenset({"authorization", "host", "cookie", "x-forwarded-for"})
_MAX_HEADER_VALUE_LENGTH = 1024


@dataclass(frozen=True)
class UpstreamResult:
    """The outcome of one upstream call, for the adapter to classify further."""

    status: int
    body: dict | None


def _revalidate_headers(headers: dict[str, str]) -> None:
    """Re-run InvokePlan's own header rules against the live dict, right before send.

    ``InvokePlan.headers`` is mutable post-construction even though the model
    itself is frozen; this closes the gap between "validated once at
    construction" and "still valid at the moment we're about to hand it to
    an HTTP client".
    """
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _FORBIDDEN_HEADER_NAMES:
            raise GatewayError(GatewayErrorCode.INTERNAL_ERROR)
        if not isinstance(value, str) or len(value) > _MAX_HEADER_VALUE_LENGTH:
            raise GatewayError(GatewayErrorCode.INTERNAL_ERROR)


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes from ``response``, streamed.

    ``chunk_size=max_bytes + 1`` bounds each pulled chunk regardless of how
    the underlying transport actually segments the body, so the loop below
    never needs more than one iteration to know whether the response is
    oversized -- it never reads (or holds) the rest of an oversized body.
    """
    buffer = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=max_bytes + 1):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            break
    return bytes(buffer)


def _parse_success_body(raw: bytes, max_bytes: int) -> dict:
    try:
        parsed = parse_strict_json(raw, max_bytes=max_bytes)
    except StrictJsonError:
        raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID) from None
    if not isinstance(parsed, dict):
        raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID)
    return parsed


def _parse_failure_body(raw: bytes, max_bytes: int) -> dict | None:
    try:
        parsed = parse_strict_json(raw, max_bytes=max_bytes)
    except StrictJsonError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


class UpstreamClient:
    """The gateway core's sole means of calling the configured upstream origin."""

    def __init__(self, config: GatewayConfig, token_manager: TokenManager, client: httpx.AsyncClient) -> None:
        self._config = config
        self._token_manager = token_manager
        self._client = client

    async def invoke(self, plan: InvokePlan) -> UpstreamResult:
        """Make the upstream call described by ``plan``, replaying at most once on 401."""
        _revalidate_headers(plan.headers)

        origin = self._config.upstream_origin
        url = f"{origin}/{plan.path}"
        if not url.startswith(origin + "/"):
            # See module docstring: unreachable given InvokePlan's own path
            # validation, kept as defense-in-depth.
            raise GatewayError(GatewayErrorCode.INTERNAL_ERROR)

        replayed = False
        while True:
            _revalidate_headers(plan.headers)

            token = await self._token_manager.get_token()
            headers = dict(plan.headers)
            headers["Authorization"] = f"Bearer {token}"
            headers["Content-Type"] = "application/json"

            request = self._client.build_request(
                "POST",
                url,
                headers=headers,
                json=plan.body,
                timeout=self._config.request_timeout_seconds,
            )
            try:
                response = await self._client.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException:
                # httpx.TimeoutException subclasses httpx.TransportError -- it
                # must be caught first, or every timeout would fall through
                # to the broader except below and be misreported as
                # upstream_unavailable.
                raise GatewayError(GatewayErrorCode.UPSTREAM_TIMEOUT) from None
            except httpx.TransportError:
                raise GatewayError(GatewayErrorCode.UPSTREAM_UNAVAILABLE) from None

            try:
                status = response.status_code

                if 300 <= status < 400:
                    raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID)

                if status == 401:
                    if replayed:
                        raise GatewayError(GatewayErrorCode.UPSTREAM_UNAUTHORIZED)
                    replayed = True
                    self._token_manager.invalidate()
                    continue

                if status == 429:
                    raise GatewayError(GatewayErrorCode.UPSTREAM_RATE_LIMITED)

                raw = await _read_capped(response, self._config.max_response_bytes)
                if 200 <= status < 300:
                    body = _parse_success_body(raw, self._config.max_response_bytes)
                else:
                    body = _parse_failure_body(raw, self._config.max_response_bytes)
                return UpstreamResult(status=status, body=body)
            finally:
                await response.aclose()
