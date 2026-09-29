"""Exact HTTP origin policy for row-selected government search endpoints."""

import pytest

from elspeth.core.security.web import SSRFBlockedError, parse_http_origin, validate_allowed_http_origin
from elspeth.plugins.transforms.blob_fetch import BlobFetchHTTPConfig
from elspeth.plugins.transforms.web_scrape import WebScrapeHTTPConfig


@pytest.mark.parametrize(
    ("configured", "requested"),
    [
        ("https://example.gov.au", "https://EXAMPLE.gov.au/search?q=one"),
        ("https://example.gov.au:443", "https://example.gov.au/search"),
        ("http://localhost:8213", "http://localhost:8213/lookup"),
    ],
)
def test_exact_origin_accepts_same_scheme_host_and_effective_port(configured: str, requested: str) -> None:
    validate_allowed_http_origin(requested, (parse_http_origin(configured),))


@pytest.mark.parametrize(
    "requested",
    [
        "http://example.gov.au/search",
        "https://example.gov.au:8443/search",
        "https://sub.example.gov.au/search",
        "https://example.gov.au.evil.test/search",
        "https://example.gov.au\\@evil.test/search",
        "https://example.gov.au:\t443/search",
    ],
)
def test_exact_origin_rejects_other_destinations(requested: str) -> None:
    with pytest.raises(SSRFBlockedError, match="origin") as exc:
        validate_allowed_http_origin(requested, (parse_http_origin("https://example.gov.au"),))
    assert exc.value.kind == "origin_not_allowed"


@pytest.mark.parametrize(
    "configured",
    [
        "https://example.gov.au/path",
        "https://example.gov.au?q=x",
        "https://example.gov.au?",
        "https://example.gov.au#",
        "https://a:b@example.gov.au",
        "ftp://example.gov.au",
        "https://example.gov.au:0",
        "https://example.gov.au:\t443",
        "https://example.gov.au%2eevil.test",
    ],
)
def test_configured_origin_must_be_an_http_origin(configured: str) -> None:
    with pytest.raises(ValueError):
        parse_http_origin(configured)


@pytest.mark.parametrize(
    ("config_type", "reason_field"),
    [(WebScrapeHTTPConfig, "scraping_reason"), (BlobFetchHTTPConfig, "fetch_reason")],
)
def test_fetch_plugin_configs_validate_exact_origins(config_type: type, reason_field: str) -> None:
    base = {"abuse_contact": "ops@example.com", reason_field: "test"}
    parsed = config_type.model_validate({**base, "allowed_origins": ["https://example.gov.au"]})
    assert parsed.allowed_origins == ("https://example.gov.au",)
    with pytest.raises(ValueError):
        config_type.model_validate({**base, "allowed_origins": ["https://example.gov.au/path"]})
    with pytest.raises(ValueError):
        config_type.model_validate({**base, "allowed_origins": ["https://example.gov.au", "https://example.gov.au:443"]})
