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
