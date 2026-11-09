"""Credential-safe, origin-bound authentication for the web scrape transform."""

from __future__ import annotations

import base64
import re
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

from elspeth.core.security.web import HTTPOrigin, parse_http_origin

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "connection",
        "content-length",
        "content-type",
        "transfer-encoding",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "accept",
        "accept-language",
        "user-agent",
        "x-requested-with",
        "x-abuse-contact",
        "x-scraping-reason",
    }
)


class WebScrapeAuthConfig(BaseModel):
    """One resolved secret, bound to one HTTPS origin and one auth scheme.

    ``credential`` is a secret-ref destination in the authored config. The
    resolver replaces the marker before plugin construction; the value is
    never put into a row or model representation.
    """

    model_config = {"extra": "forbid", "hide_input_in_errors": True}

    scheme: Literal["basic", "bearer", "api_key"]
    origin: str = Field(description="Exact HTTPS origin receiving the credential.")
    credential: SecretStr = Field(repr=False, description="Resolved secret; author as a secret reference.")
    header_name: str | None = Field(default=None, description="API-key header name; defaults to X-API-Key.")

    @model_validator(mode="after")
    def _validate_binding(self) -> WebScrapeAuthConfig:
        origin = parse_http_origin(self.origin)
        if origin[0] != "https":
            raise ValueError("auth.origin must use HTTPS")
        if self.scheme != "api_key" and self.header_name is not None:
            raise ValueError("auth.header_name is only valid for api_key")
        if self.scheme == "api_key":
            header_name = self.header_name or "X-API-Key"
            if _HEADER_NAME.fullmatch(header_name) is None or header_name.casefold() in _RESERVED_HEADERS:
                raise ValueError("auth.header_name is not an allowed API-key header")
        return self

    @property
    def parsed_origin(self) -> HTTPOrigin:
        return parse_http_origin(self.origin)

    def header(self) -> tuple[str, str]:
        """Build one wire header without exposing secret bytes in exceptions."""
        credential = self.credential.get_secret_value()
        if not credential or len(credential) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in credential):
            raise ValueError("auth credential is empty, oversized, or not a safe ASCII header value")
        if self.scheme == "bearer" and " " in credential:
            raise ValueError("auth bearer credential must not contain spaces")
        if self.scheme == "basic":
            encoded = base64.b64encode(credential.encode("ascii")).decode("ascii")
            return "Authorization", f"Basic {encoded}"
        if self.scheme == "bearer":
            return "Authorization", f"Bearer {credential}"
        return self.header_name or "X-API-Key", credential
