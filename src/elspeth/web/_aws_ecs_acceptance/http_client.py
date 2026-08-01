"""Bounded HTTP transport for AWS ECS acceptance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from .contracts import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_BLOB_RESPONSE_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    POOL_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    WRITE_TIMEOUT_SECONDS,
    AcceptanceCheckError,
    AcceptanceHttpError,
    AcceptanceInputError,
    acceptance_step,
    normalize_acceptance_origin,
)
from .state import AcceptanceCredentials

_NO_BODY = object()

# Operator-supplied trust-bundle environment variables the underlying HTTP
# stack honors.  An unreadable bundle otherwise only surfaces as an opaque
# connect failure (or an internal error at client construction), which cost
# real debugging time during the 2026-07-30 cold install (F12).
# HTTPX consumes SSL_CERT_FILE through its trust-environment handling. The
# requests-specific REQUESTS_CA_BUNDLE variable must not block this client.
_CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE",)


def _require_readable_ca_bundle(env: Mapping[str, str]) -> None:
    """Fail closed with a named code when a declared CA bundle is unreadable."""

    for name in _CA_BUNDLE_ENV_VARS:
        value = env.get(name)
        if not value:
            continue
        try:
            with open(value, "rb") as handle:
                handle.read(1)
        except OSError:
            raise AcceptanceHttpError(
                f"acceptance CA bundle named by {name} is not readable",
                error_code="ca_unreadable",
            ) from None


class AcceptanceHttpClient:
    """Bounded, no-redirect, same-origin HTTP client for acceptance checks."""

    def __init__(
        self,
        *,
        origin: str,
        credentials: AcceptanceCredentials,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.origin = origin
        self.credentials = credentials
        self._bearer_token = credentials.bearer_token
        with acceptance_step("client_setup"):
            try:
                self._client = httpx.Client(
                    base_url=origin,
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        connect=CONNECT_TIMEOUT_SECONDS,
                        read=READ_TIMEOUT_SECONDS,
                        write=WRITE_TIMEOUT_SECONDS,
                        pool=POOL_TIMEOUT_SECONDS,
                    ),
                    transport=transport,
                )
            except OSError:
                # The only filesystem the client touches at construction is
                # the TLS trust store; an unreadable operator CA bundle is
                # the known cause (F12).
                raise AcceptanceHttpError(
                    "acceptance TLS trust store could not be loaded",
                    error_code="ca_unreadable",
                ) from None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Self:
        with acceptance_step("client_setup"):
            _require_readable_ca_bundle(env)
            origin = normalize_acceptance_origin(env.get("ELSPETH_ACCEPTANCE_BASE_URL", ""))
            credentials = AcceptanceCredentials.from_env(env)
        return cls(origin=origin, credentials=credentials, transport=transport)

    def __enter__(self) -> Self:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json_body: object = _NO_BODY,
    ) -> object:
        """Send one bounded request and decode a bounded JSON response."""

        _status, decoded = self.request_json_with_status(
            method,
            path,
            expected_statuses=expected_statuses,
            json_body=json_body,
        )
        return decoded

    def request_json_with_status(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json_body: object = _NO_BODY,
    ) -> tuple[int, object]:
        status, content = self._request_bounded(
            method,
            path,
            expected_statuses=expected_statuses,
            json_body=json_body,
            limit=MAX_JSON_RESPONSE_BYTES,
        )
        try:
            return status, json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AcceptanceHttpError(
                "acceptance response contained malformed JSON",
                error_code="response_shape_invalid",
            ) from None

    def request_multipart_json(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> object:
        _status, content = self._request_bounded(
            method,
            path,
            expected_statuses=expected_statuses,
            files=files,
            limit=MAX_JSON_RESPONSE_BYTES,
        )
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AcceptanceHttpError(
                "acceptance response contained malformed JSON",
                error_code="response_shape_invalid",
            ) from None

    def request_bytes(self, method: str, path: str, *, expected_statuses: set[int]) -> bytes:
        _status, content = self._request_bounded(
            method,
            path,
            expected_statuses=expected_statuses,
            limit=MAX_BLOB_RESPONSE_BYTES,
        )
        return content

    def authenticate(self, *, register: bool) -> None:
        """Resolve local credentials to a token, or retain the supplied bearer token."""

        if self.credentials.mode == "bearer":
            if register:
                raise AcceptanceInputError("registration is available only for local acceptance authentication")
            return
        username = self.credentials.username
        password = self.credentials.password
        if username is None or password is None:
            raise AcceptanceInputError("local acceptance authentication is incomplete")

        if register:
            with acceptance_step("register"):
                status, body = self.request_json_with_status(
                    "POST",
                    "/api/auth/register",
                    expected_statuses={200, 409},
                    json_body={"username": username, "password": password, "display_name": username},
                )
                if status == 200:
                    self._accept_auth_token(body)
                    return
        with acceptance_step("login"):
            body = self.request_json(
                "POST",
                "/api/auth/login",
                expected_statuses={200},
                json_body={"username": username, "password": password},
            )
            self._accept_auth_token(body)

    def _accept_auth_token(self, body: object) -> None:
        if not isinstance(body, dict):
            raise AcceptanceCheckError("authentication_response")
        token = body.get("access_token")
        token_type = body.get("token_type")
        if type(token) is not str or not token or len(token) > 64 * 1024 or token_type != "bearer":
            raise AcceptanceCheckError("authentication_response")
        self._bearer_token = token

    def _request_bounded(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json_body: object = _NO_BODY,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        limit: int,
    ) -> tuple[int, bytes]:
        if not path.startswith("/") or path.startswith("//"):
            raise AcceptanceInputError("acceptance request requires a relative API path")
        parsed_path = urlsplit(path)
        if parsed_path.scheme or parsed_path.netloc:
            raise AcceptanceInputError("acceptance request requires a relative API path")

        headers: dict[str, str] = {}
        if self._bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not _NO_BODY:
            kwargs["json"] = json_body
        if files is not None:
            kwargs["files"] = files
        request = self._client.build_request(method, path, **kwargs)
        try:
            response = self._client.send(request, stream=True)
            try:
                self.validate_response_origin(response.request.url)
                content = self._read_bounded(response, limit=limit)
                if response.status_code not in expected_statuses:
                    raise AcceptanceHttpError(
                        "acceptance request returned an unexpected HTTP status",
                        error_code="unexpected_http_status",
                        status=response.status_code,
                    )
                status = response.status_code
            finally:
                response.close()
        except AcceptanceHttpError:
            raise
        except httpx.TimeoutException:
            raise AcceptanceHttpError("acceptance request timeout", error_code="request_timeout") from None
        except httpx.HTTPError:
            raise AcceptanceHttpError("acceptance HTTP transport failed", error_code="connection_failed") from None
        return status, content

    def validate_response_origin(self, url: httpx.URL) -> None:
        """Require an HTTP response URL to remain on the configured origin."""

        try:
            response_origin = normalize_acceptance_origin(f"{url.scheme}://{url.netloc.decode('ascii')}")
        except AcceptanceInputError:
            raise AcceptanceHttpError("acceptance response was cross-origin", error_code="cross_origin_response") from None
        if response_origin != self.origin:
            raise AcceptanceHttpError("acceptance response was cross-origin", error_code="cross_origin_response")

    @staticmethod
    def _read_bounded(response: httpx.Response, *, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise AcceptanceHttpError("acceptance response body was too large", error_code="response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
