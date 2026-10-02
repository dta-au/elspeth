"""Admission of links discovered in untrusted HTML before row persistence."""

from unittest.mock import patch

import pytest

from elspeth.core.security.web import parse_http_origin
from elspeth.plugins.transforms.web_scrape_links import resolve_discovered_href


def test_relative_href_uses_final_response_url_and_strips_fragment() -> None:
    with patch("socket.getaddrinfo", side_effect=AssertionError("link normalization must not resolve DNS")):
        result = resolve_discovered_href(
            "../detail/42?view=full#staff",
            base_url="https://register.example.gov/search/results/page?search=old",
            allowed_origins=(parse_http_origin("https://register.example.gov"),),
            allowed_ranges=(),
        )
    assert result == "https://register.example.gov/search/detail/42?view=full"


@pytest.mark.parametrize(
    ("href", "allowed_origin"),
    [
        ("https://other.example.gov/detail/42", "https://register.example.gov"),
        ("//other.example.gov/detail/42", "https://register.example.gov"),
        ("javascript:alert(1)", "https://register.example.gov"),
        ("https://user:pass@register.example.gov/detail/42", "https://register.example.gov"),
        ("http://169.254.169.254/latest/meta-data/", "http://169.254.169.254"),
        ("/detail/42?access_token=secret-value", "https://register.example.gov"),
        ("/detail/42\r\nHost: other.example.gov", "https://register.example.gov"),
    ],
)
def test_discovered_href_refuses_off_policy_and_secret_targets_without_echo(href: str, allowed_origin: str) -> None:
    with pytest.raises(ValueError) as caught:
        resolve_discovered_href(
            href,
            base_url="https://register.example.gov/search/results",
            allowed_origins=(parse_http_origin(allowed_origin),),
            allowed_ranges=(),
        )
    assert href not in str(caught.value)
    assert "secret-value" not in str(caught.value)


def test_discovered_href_requires_declared_origin() -> None:
    with pytest.raises(ValueError, match="allowed_origins"):
        resolve_discovered_href(
            "/detail/42",
            base_url="https://register.example.gov/search/results",
            allowed_origins=(),
            allowed_ranges=(),
        )
