"""Single-flight OAuth2 client-credentials token lifecycle.

``TokenManager`` is the gateway core's only OAuth client: adapters never see
tokens, request the token URL, or hold the client credentials — they call
into the gateway core, which attaches whatever ``Authorization`` header the
upstream adapter needs. A single ``asyncio.Lock`` protects the fetch path so
concurrent callers on a cold or expired cache collapse into exactly one
token request; every waiter re-checks the cache after acquiring the lock so
it can pick up whatever the winner just fetched instead of firing its own
duplicate request.

Every failure class — a non-200 response, a missing/empty ``access_token``,
a ``token_type`` that is not case-insensitively ``"bearer"``, an invalid or
absent-with-no-fallback ``expires_in``, a non-JSON or non-object body, or a
transport-level error — raises the same
``GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)``. ``GatewayError``
carries no free text (see ``core.errors``), so nothing from the token
response — not the body, not the client id or secret — can ever reach a
caller through this path. Nothing in this module logs any of that material
either; keep it that way.
"""

import asyncio
import base64
import time
from collections.abc import Callable

import httpx

from elspeth_llm_gateway.core.config import GatewayConfig
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json

_LOOPBACK_PREFIX = "http://127.0.0.1"


def _is_valid_token_url(url: str) -> bool:
    """``https://...`` or exactly ``http://127.0.0.1`` (host boundary, not a substring).

    A plain ``str.startswith("http://127.0.0.1")`` would also accept
    ``http://127.0.0.1.evil.com/token`` — the prefix matches, but the host is
    not loopback at all. Requiring the character immediately after the
    prefix to be absent, ``:``, or ``/`` closes that hole.
    """
    if url.startswith("https://"):
        return True
    if not url.startswith(_LOOPBACK_PREFIX):
        return False
    rest = url[len(_LOOPBACK_PREFIX) :]
    return rest == "" or rest[0] in (":", "/")


class TokenManager:
    """Fetches and caches an OAuth2 client-credentials access token.

    ``clock`` is injectable (defaults to ``time.monotonic``) so expiry/skew
    behaviour can be pinned deterministically in tests.
    """

    def __init__(
        self,
        config: GatewayConfig,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _is_valid_token_url(config.oauth_token_url):
            raise ValueError("oauth_token_url must start with 'https://' or 'http://127.0.0.1'")

        self._config = config
        self._client = client
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None

    def invalidate(self) -> None:
        """Drop the cached token, forcing the next ``get_token`` to fetch."""
        self._token = None
        self._expires_at = None

    async def get_token(self) -> str:
        """Return a valid access token, fetching (single-flight) if needed."""
        if self._is_cached_valid():
            return self._token

        async with self._lock:
            # A waiter that queued behind another caller's fetch must reuse
            # whatever that fetch just cached, not fire a duplicate request.
            if self._is_cached_valid():
                return self._token

            token, expires_at = await self._fetch()
            self._token = token
            self._expires_at = expires_at
            return token

    def _is_cached_valid(self) -> bool:
        if self._token is None or self._expires_at is None:
            return False
        return self._clock() < self._expires_at - self._config.refresh_skew_seconds

    async def _fetch(self) -> tuple[str, float]:
        data: dict[str, str] = {"grant_type": "client_credentials"}
        if self._config.oauth_scopes:
            data["scope"] = " ".join(self._config.oauth_scopes)

        headers: dict[str, str] = {}
        if self._config.oauth_auth_method == "client_secret_basic":
            credentials = f"{self._config.oauth_client_id.get_secret_value()}:{self._config.oauth_client_secret.get_secret_value()}"
            headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        else:
            data["client_id"] = self._config.oauth_client_id.get_secret_value()
            data["client_secret"] = self._config.oauth_client_secret.get_secret_value()

        request_time = self._clock()
        try:
            response = await self._client.post(
                self._config.oauth_token_url,
                data=data,
                headers=headers,
                timeout=self._config.request_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE) from None

        if response.status_code != 200:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)

        try:
            payload = parse_strict_json(response.content, max_bytes=self._config.max_response_bytes)
        except StrictJsonError:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE) from None

        if not isinstance(payload, dict):
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)

        access_token = self._require_access_token(payload)
        self._require_bearer_token_type(payload)
        lifetime = self._resolve_lifetime(payload)

        return access_token, request_time + lifetime

    @staticmethod
    def _require_access_token(payload: dict) -> str:
        if "access_token" not in payload:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)
        access_token = payload["access_token"]
        if not isinstance(access_token, str) or access_token == "":
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)
        return access_token

    @staticmethod
    def _require_bearer_token_type(payload: dict) -> None:
        if "token_type" not in payload:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)
        token_type = payload["token_type"]
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)

    def _resolve_lifetime(self, payload: dict) -> int:
        if "expires_in" not in payload:
            if self._config.oauth_fixed_lifetime_seconds is None:
                raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)
            return self._config.oauth_fixed_lifetime_seconds

        expires_in = payload["expires_in"]
        # `bool` is a subclass of `int` in Python, so an `isinstance` check
        # would silently accept `expires_in: true` as `1`; `type(...) is int`
        # rejects that alongside floats and strings.
        if type(expires_in) is not int or expires_in <= 0:
            raise GatewayError(GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE)
        return expires_in
