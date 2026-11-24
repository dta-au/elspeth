"""Provider failures exactly as ``litellm.acompletion`` raises them.

Each failure is produced by a real ``litellm.acompletion`` call whose HTTP
traffic goes to an ``httpx.MockTransport``, with placeholder credentials and no
network. The provider handler's own rewrap and LiteLLM's exception mapper both
run, so the exception has the class, status and headers the Composer receives
on that route. Hand-built LiteLLM exceptions, or exceptions fed straight to the
mapper, can carry fields this path never delivers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

import httpx
import openai
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.main import acompletion as _litellm_acompletion  # LiteLLM's own function, not a test's patched package attribute

Route = Literal["openai", "anthropic", "openrouter", "bedrock"]
Failure = Literal["429_retry_after", "429_quota", "403", "500", "503", "connect", "read", "malformed_200"]

ROUTES: tuple[Route, ...] = ("openai", "anthropic", "openrouter", "bedrock")
FAILURES: tuple[Failure, ...] = ("429_retry_after", "429_quota", "403", "500", "503", "connect", "read", "malformed_200")

_JSON = {"content-type": "application/json"}
_DEFAULT_RETRY_AFTER: Mapping[str, str] = {"retry-after": "7"}
_MODELS: dict[Route, str] = {
    "openai": "openai/gpt-4o",
    "anthropic": "anthropic/claude-3-5-sonnet-20241022",
    "openrouter": "openrouter/openai/gpt-4o",
    "bedrock": "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
}


def _responder(failure: Failure, retry_after_headers: Mapping[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "429_retry_after":
            return httpx.Response(
                429, headers={**_JSON, **retry_after_headers}, json={"error": {"message": "slow down", "type": "rate_limit"}}
            )
        if failure == "429_quota":
            body = {"code": "insufficient_quota", "type": "insufficient_quota", "message": "quota"}
            return httpx.Response(429, headers=_JSON, json={"error": body})
        if failure == "403":
            return httpx.Response(403, headers=_JSON, json={"error": {"message": "no access", "type": "permission_error"}})
        if failure == "500":
            return httpx.Response(500, headers=_JSON, json={"error": {"message": "internal", "type": "server_error"}})
        if failure == "503":
            return httpx.Response(503, headers=_JSON, json={"error": {"message": "unavailable", "type": "server_error"}})
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if failure == "read":
            raise httpx.ReadError("reset mid-body", request=request)
        return httpx.Response(200, content=b"{not json", headers=_JSON)

    return respond


async def provider_failure(route: Route, failure: Failure, *, retry_after_headers: Mapping[str, str] = _DEFAULT_RETRY_AFTER) -> Exception:
    """Return the exception ``litellm.acompletion`` raises for ``failure`` on ``route``.

    ``retry_after_headers`` replaces the headers a ``429_retry_after`` response carries.
    """
    transport = httpx.MockTransport(_responder(failure, retry_after_headers))
    messages = [{"role": "user", "content": "hi"}]
    try:
        if route == "openai":
            client = openai.AsyncOpenAI(
                api_key="placeholder",
                base_url="http://provider.mock.invalid/v1",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=transport),
            )
            await _litellm_acompletion(model=_MODELS[route], messages=messages, client=client, num_retries=0, max_retries=0)
        else:
            handler = AsyncHTTPHandler()
            handler.client = httpx.AsyncClient(transport=transport)
            credentials: dict[str, str] = (
                {"aws_access_key_id": "placeholder", "aws_secret_access_key": "placeholder", "aws_region_name": "us-east-1"}
                if route == "bedrock"
                else {"api_key": "placeholder"}
            )
            await _litellm_acompletion(
                model=_MODELS[route],
                messages=messages,
                client=handler,
                api_base="http://provider.mock.invalid",
                num_retries=0,
                max_retries=0,
                **credentials,
            )
    except Exception as raised:  # the raised exception is the value under test
        return raised
    raise AssertionError(f"litellm.acompletion returned instead of raising for {route}/{failure}")
