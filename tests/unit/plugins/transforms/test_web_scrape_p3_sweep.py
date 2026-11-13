"""Regression tests for the 20261111-plugins-review P3 web_scrape findings."""

import pytest

from elspeth.core.security.web import parse_http_origin
from elspeth.plugins.transforms.web_scrape_links import resolve_discovered_href

_BASE = "https://reg.example.gov/s"
_ORIGINS = (parse_http_origin("https://reg.example.gov"),)


def _resolve(href: str) -> str:
    return resolve_discovered_href(href, base_url=_BASE, allowed_origins=_ORIGINS, allowed_ranges=())


# Finding #160: the admitted link is emitted with the page's own query bytes.


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/d?flag", "https://reg.example.gov/d?flag"),
        ("/d?a=1;b=2", "https://reg.example.gov/d?a=1;b=2"),
        ("/d?ids=1,2", "https://reg.example.gov/d?ids=1,2"),
        ("/d?q=100%", "https://reg.example.gov/d?q=100%"),
        ("/d?view=full&page=2#frag", "https://reg.example.gov/d?view=full&page=2"),
    ],
)
def test_discovered_href_query_is_emitted_unchanged(href: str, expected: str) -> None:
    assert _resolve(href) == expected


@pytest.mark.parametrize("href", ["/d?a=1;token=secret-value", "/d?a=1;access_token=secret-value"])
def test_discovered_href_refuses_sensitive_parameter_after_semicolon(href: str) -> None:
    # Once the raw query is emitted, a server that splits on ';' would see
    # the credential parameter, so the admission check must see it too.
    with pytest.raises(ValueError) as caught:
        _resolve(href)
    assert "secret-value" not in str(caught.value)


# Finding #161: discovered hrefs and Link headers share one capability-key policy.


def test_capability_query_keys_have_one_shared_definition() -> None:
    from elspeth.plugins.infrastructure.clients import fingerprinting

    assert hasattr(fingerprinting, "is_capability_query_param")
    for key in ("session", "State", "cursor", "continuation", "next_token", "page_token"):
        assert fingerprinting.is_capability_query_param(key)
    assert not fingerprinting.is_capability_query_param("view")


@pytest.mark.parametrize(
    "href",
    [
        "/d?session=secret-value",
        "/d?Session=secret-value",
        "/d?state=secret-value&code=abc",
        "/d?cursor=secret-value",
        "/d?continuation=secret-value",
        "/d?next_token=secret-value",
        "/d?a=1;session=secret-value",
    ],
)
def test_discovered_href_refuses_capability_query_keys(href: str) -> None:
    with pytest.raises(ValueError) as caught:
        _resolve(href)
    assert "secret-value" not in str(caught.value)


# Finding #152: a JSON record column without a path is refused at config time.


@pytest.mark.parametrize("column", [{"field": "name"}, {"field": "name", "path": []}])
def test_json_record_column_requires_nonempty_path(column: dict[str, object]) -> None:
    from pydantic import ValidationError

    from elspeth.plugins.transforms.web_scrape_json_extraction import JSONRecordsConfig

    with pytest.raises(ValidationError, match="path"):
        JSONRecordsConfig.model_validate({"field": "r", "records_path": ["results"], "columns": [column]})


# Finding #5: fingerprint_mode is described as what compute_fingerprint does.


def test_fingerprint_mode_description_matches_compute_fingerprint() -> None:
    import hashlib

    from elspeth.plugins.transforms.web_scrape import WebScrapeConfig
    from elspeth.plugins.transforms.web_scrape_fingerprint import compute_fingerprint

    description = WebScrapeConfig.model_fields["fingerprint_mode"].description
    assert description is not None
    # Both modes hash only the extracted content; 'full' just skips whitespace
    # normalization, so the contract must not promise response context.
    assert compute_fingerprint("a  b", "full") == hashlib.sha256(b"a  b").hexdigest()
    assert "response" not in description.lower()
    assert "whitespace" in description.lower()


# Finding #163: authenticated web_scrape is refused before any row is processed.


def _authenticated_options(credential: str) -> dict[str, object]:
    return {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "content_field": "response_text",
        "fingerprint_field": "response_fingerprint",
        "format": "raw",
        "auth": {"scheme": "bearer", "origin": "https://example.com", "credential": credential},
        "http": {
            "abuse_contact": "test@example.com",
            "scraping_reason": "Read-only public data retrieval",
            "allowed_origins": ["https://example.com"],
        },
    }


def test_authenticated_web_scrape_is_refused_at_config_validation() -> None:
    from elspeth.plugins.infrastructure.validation import validate_transform_config

    credential = "CUSTODY42"
    errors = validate_transform_config("web_scrape", _authenticated_options(credential))
    assert any("response evidence is secret safe" in error.message for error in errors)
    assert credential not in repr(errors)


def test_authenticated_web_scrape_construction_fails_without_echoing_credential() -> None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

    credential = "CUSTODY42"
    with pytest.raises(PluginConfigError) as caught:
        WebScrapeTransform(_authenticated_options(credential))
    assert "response evidence is secret safe" in str(caught.value)
    assert credential not in str(caught.value)
    assert credential not in str(caught.value.__cause__)
