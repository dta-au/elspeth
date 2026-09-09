"""Deterministic mock OAuth client-credentials token endpoint.

Mirrors the RFC 6749 client-credentials grant the gateway's own
``TokenManager`` (``elspeth_llm_gateway.core.oauth``) speaks against a real
agency: ``POST /token`` accepts both ``client_secret_basic`` (HTTP Basic,
each of ``client_id``/``client_secret`` form-urlencoded via ``quote_plus``
*before* being joined with ``:`` and base64-encoded, per RFC 6749 Appendix
B -- not a raw ``base64(id:secret)``) and ``client_secret_post`` (the two
credentials sent as plain form fields).

Tokens are issued as a strictly sequential counter -- ``mock-token-1``,
``mock-token-2``, ... -- with no randomness and no wall-clock dependence, so
every response is a pure function of call order alone.
"""

import base64
import binascii
from itertools import count
from urllib.parse import unquote_plus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_DEFAULT_CLIENT_ID = "mock-client"
# Development-only fixed mock secret; never used against a real credential store.
_DEFAULT_CLIENT_SECRET = "mock-secret-0123456789abcdef0123456789abcdef"  # secret-scan: allow-this-line

_BASIC_PREFIX = "Basic "


def _decode_basic_credentials(header: str) -> tuple[str, str] | None:
    """Undo the client's RFC 6749 Appendix B Basic encoding, or ``None`` if malformed.

    The client form-urlencodes each credential (``quote_plus``, which
    percent-encodes ``:``) before joining with ``:`` and base64-encoding, so
    splitting the decoded string on the *first* ``:`` unambiguously
    separates the two still-encoded parts.
    """
    if not header.startswith(_BASIC_PREFIX):
        return None
    encoded = header[len(_BASIC_PREFIX) :]
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return unquote_plus(username), unquote_plus(password)


def create_mock_oauth_app(
    *,
    client_id: str = _DEFAULT_CLIENT_ID,
    client_secret: str = _DEFAULT_CLIENT_SECRET,
    expires_in: int = 3600,
    fail_next: dict | None = None,
) -> FastAPI:
    """Build the mock OAuth token-issuing app.

    ``fail_next={"status": <code>}`` makes exactly the *next* ``/token``
    call return that status with no token issued and the sequential
    counter untouched; the pending failure is consumed on that call, so
    every call after it behaves normally again.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    token_counter = count(1)
    pending_failure: dict | None = fail_next

    @app.post("/token")
    async def issue_token(request: Request) -> JSONResponse:
        nonlocal pending_failure
        if pending_failure is not None:
            status = pending_failure["status"]
            pending_failure = None
            return JSONResponse(status_code=status, content={"error": "server_error"})

        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

        auth_header = request.headers.get("authorization")
        if auth_header is not None:
            credentials = _decode_basic_credentials(auth_header)
            if credentials is None:
                return JSONResponse(status_code=401, content={"error": "invalid_client"})
            supplied_id, supplied_secret = credentials
        else:
            supplied_id = form.get("client_id")
            supplied_secret = form.get("client_secret")

        if supplied_id != client_id or supplied_secret != client_secret:
            return JSONResponse(status_code=401, content={"error": "invalid_client"})

        access_token = f"mock-token-{next(token_counter)}"
        return JSONResponse(
            status_code=200,
            content={"access_token": access_token, "token_type": "bearer", "expires_in": expires_in},
        )

    return app
