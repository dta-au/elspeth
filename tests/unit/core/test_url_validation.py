"""Tests for credential-bearing plugin URL validation."""

from __future__ import annotations

import traceback

import pytest

from elspeth.core.url_validation import validate_credential_safe_https_url


@pytest.mark.parametrize("url", ["https://example.test", "https://example.test/path?q=1#fragment", "HTTPS://example.test:443/"])
def test_credential_url_retains_existing_https_acceptance(url: str) -> None:
    assert validate_credential_safe_https_url(url, field_name="endpoint") == url


def test_malformed_authority_error_has_no_external_chain() -> None:
    secret = "p5-core-" + "runtime-password"
    with pytest.raises(ValueError, match="endpoint must be a valid URL") as caught:
        validate_credential_safe_https_url(f"https://user:{secret}@exam\uff0fple.test", field_name="endpoint")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8199/v1",
        "http://127.0.0.1:8199/v1",
        "http://127.0.0.42:8199/v1",
        "http://[::1]:8199/v1",
        "http://[0:0:0:0:0:0:0:1]:8199/v1",
    ],
)
def test_credential_url_allows_explicit_http_loopback_when_opted_in(url: str) -> None:
    assert validate_credential_safe_https_url(url, field_name="base_url", allow_http_loopback=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "http://127.0.0.999:8199/v1",
        "http://127.example.com/v1",
        "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11:8199/v1",
    ],
)
def test_credential_url_rejects_non_loopback_http_when_opted_in(url: str) -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_credential_safe_https_url(url, field_name="base_url", allow_http_loopback=True)
