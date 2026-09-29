"""Pagination keeps untrusted links bounded and uses audited SSRF-safe GETs."""

import json
import socket
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from xml.etree import ElementTree

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import Call
from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.web_scrape import WebScrapeTransform
from elspeth.plugins.transforms.web_scrape_pagination import PaginationConfig, discover_next_url
from elspeth.testing import make_pipeline_row
from tests.fixtures.mock_audit import mock_item_audit_authority

_IP = "104.18.27.120"


class _Recorder:
    def __init__(self) -> None:
        self.count = 0

    def allocate_call_index(self, *_args: Any, **_kwargs: Any) -> int:
        return self.count

    def record_call(self, *_args: Any, **_kwargs: Any) -> Call:
        self.count += 1
        return Call(
            call_id=f"call-{self.count}",
            call_index=self.count - 1,
            call_type=CallType.HTTP,
            status=CallStatus.SUCCESS,
            request_hash=f"request-{self.count}",
            created_at=datetime.now(UTC),
            state_id="state-123",
            request_ref=f"request-ref-{self.count}",
            response_hash=f"response-{self.count}",
            response_ref=f"response-ref-{self.count}",
            latency_ms=1.0,
        )


class _PayloadStore:
    def store(self, _payload: bytes) -> str:
        return "processed-ref"


class _LimiterRegistry:
    def get_limiter(self, _name: str) -> None:
        return None


@pytest.fixture
def context() -> PluginContext:
    return PluginContext(
        run_id="run-123",
        **mock_item_audit_authority("run-123"),
        config={},
        landscape=_Recorder(),
        payload_store=_PayloadStore(),
        rate_limit_registry=_LimiterRegistry(),
        state_id="state-123",
    )


def _dns(host: str, _port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    assert host == "example.com"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP, 0))]


def _transform(
    mode: str, *, max_pages: int = 5, max_total_body_bytes: int = 10000, max_elapsed_seconds: float = 120.0
) -> WebScrapeTransform:
    pagination: dict[str, object] = {
        "mode": mode,
        "max_pages": max_pages,
        "max_total_body_bytes": max_total_body_bytes,
        "max_elapsed_seconds": max_elapsed_seconds,
    }
    if mode == "next_link_css":
        pagination["next_link_selector"] = "a.next"
    return WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "text",
            "pagination": pagination,
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Pagination unit test",
                "allowed_origins": ["https://example.com"],
            },
        }
    )


@pytest.mark.parametrize("mode", ["next_link_css", "link_header"])
@respx.mock
def test_two_pages_are_audited_and_aggregated(context: PluginContext, mode: str) -> None:
    first_headers = {"content-type": "text/html"}
    first_body = '<p>first</p><a class="next" href="/page-2">Next</a>'
    if mode == "link_header":
        first_headers["link"] = '</page-2>; rel="next"'
    route1 = respx.get(f"https://{_IP}:443/page-1").mock(return_value=httpx.Response(200, headers=first_headers, text=first_body))
    route2 = respx.get(f"https://{_IP}:443/page-2").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<p>second</p>")
    )
    transform = _transform(mode)
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)

    assert result.status == "success"
    assert route1.called and route2.called
    assert context.landscape.count == 2
    assert result.row["content"] == "first Next second"
    assert result.row["fetch_url_final"] == "https://example.com/page-2"
    pages = result.success_reason["metadata"]["pagination_pages"]
    assert [page["number"] for page in pages] == [1, 2]
    assert [page["request_ref"] for page in pages] == ["request-ref-1", "request-ref-2"]
    assert result.success_reason["metadata"]["pagination_stop_reason"] == "no_next_link"


@respx.mock
def test_one_page_still_obeys_elapsed_limit(context: PluginContext) -> None:
    def slow_response(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>only page</p>")

    respx.get(f"https://{_IP}:443/page-1").mock(side_effect=slow_response)
    transform = _transform("next_link_css", max_elapsed_seconds=0.001)
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)

    assert result.status == "error"
    assert result.reason["reason"] == "pagination_limit_exceeded"
    assert context.landscape.count == 1


@respx.mock
def test_records_and_structured_provenance_expand_together_across_pages(context: PluginContext) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<main><li>Agency One</li><a class="next" href="/page-2">Next</a></main>',
        )
    )
    respx.get(f"https://{_IP}:443/page-2").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<main><li>Agency Two</li></main>")
    )
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "text",
            "records": {
                "field": "candidates",
                "provenance_field": "candidate_sources",
                "selector": "main li",
                "columns": [{"field": "name", "required": True}],
            },
            "pagination": {"mode": "next_link_css", "next_link_selector": "a.next"},
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Pagination provenance test",
                "allowed_origins": ["https://example.com"],
            },
        }
    )
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "success"
    assert [entry["name"] for entry in result.row["candidates"]] == ["Agency One", "Agency Two"]
    assert [entry["name"]["source_url"] for entry in result.row["candidate_sources"]] == [
        "https://example.com/page-1",
        "https://example.com/page-2",
    ]


@respx.mock
def test_cycle_stops_without_refetch(context: PluginContext) -> None:
    route = respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html", "link": '</page-1>; rel="next"'}, text="first")
    )
    transform = _transform("link_header")
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "success"
    assert route.call_count == 1
    assert result.success_reason["metadata"]["pagination_stop_reason"] == "cycle"


@respx.mock
def test_page_limit_stops_before_next_request(context: PluginContext) -> None:
    route = respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html", "link": '</page-2>; rel="next"'}, text="first")
    )
    transform = _transform("link_header", max_pages=1)
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "success"
    assert route.call_count == 1
    assert result.success_reason["metadata"]["pagination_stop_reason"] == "max_pages"


@respx.mock
def test_cross_origin_next_link_is_refused_before_dns(context: PluginContext) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html", "link": '<https://other.test/private>; rel="next"'}, text="first"
        )
    )
    transform = _transform("link_header")
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "error"
    assert result.reason["reason"] == "validation_failed"
    assert context.landscape.count == 1


@respx.mock
def test_aggregate_body_limit_refuses_second_page(context: PluginContext) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html", "link": '</page-2>; rel="next"'}, text="first")
    )
    respx.get(f"https://{_IP}:443/page-2").mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="second"))
    transform = _transform("link_header", max_total_body_bytes=10)
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "error"
    assert result.reason["reason"] == "pagination_limit_exceeded"
    assert context.landscape.count == 2


def test_pagination_requires_get_and_explicit_origin() -> None:
    base: dict[str, object] = {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "content_field": "content",
        "fingerprint_field": "fingerprint",
        "pagination": {"mode": "link_header"},
        "http": {"abuse_contact": "test@example.com", "scraping_reason": "test"},
    }
    with pytest.raises(PluginConfigError, match="allowed_origins"):
        WebScrapeTransform(base)
    base["http"] = {"abuse_contact": "test@example.com", "scraping_reason": "test", "allowed_origins": ["https://example.com"]}
    base["method"] = "POST"
    base["request_json_field"] = "body"
    with pytest.raises(PluginConfigError, match=r"pagination.*GET"):
        WebScrapeTransform(base)


def test_discovery_rejects_ambiguous_links_and_strips_fragments() -> None:
    config = PaginationConfig(mode="next_link_css", next_link_selector="a.next")
    response = httpx.Response(200, headers={"content-type": "text/html"}, text='<a class="next" href="../more#top">More</a>')
    assert discover_next_url(response, "https://example.com/search/page-1", config) == "https://example.com/more"
    duplicate = httpx.Response(
        200, headers={"content-type": "text/html"}, text='<a class="next" href="/a"></a><a class="next" href="/b"></a>'
    )
    with pytest.raises(ValueError, match="multiple"):
        discover_next_url(duplicate, "https://example.com/", config)
    link_config = PaginationConfig(mode="link_header")
    ambiguous = httpx.Response(200, headers={"link": '</a>; rel="next", </b>; rel="next"'})
    with pytest.raises(ValueError, match="multiple"):
        discover_next_url(ambiguous, "https://example.com/", link_config)


@pytest.mark.parametrize(
    "value",
    [
        '</page-2?token=secret>; rel="next"',
        '</page-2?cursor=opaque>; rel="next"',
        '</page-2?q=Acme%20Co>; rel="next"',
        '</page-2>; rel="next"; title="more"',
        '<https://example.com/page-2>; rel="foo-bar next"',
        '<https://example.com/page-2>; rel="foo_1 next"',
    ],
)
def test_link_mode_refuses_headers_without_exact_replay_transport(value: str) -> None:
    response = httpx.Response(200, headers={"link": value})
    with pytest.raises(ValueError):
        discover_next_url(response, "https://example.com/page-1", PaginationConfig(mode="link_header"))


def test_link_mode_keeps_public_search_query_and_numeric_page() -> None:
    response = httpx.Response(200, headers={"link": '</page-2?SearchText=Acme+Co&page=2>; rel="next"'})
    assert (
        discover_next_url(response, "https://example.com/page-1", PaginationConfig(mode="link_header"))
        == "https://example.com/page-2?SearchText=Acme+Co&page=2"
    )


@pytest.mark.parametrize(
    "target",
    [
        "http://user:pass@example.com/page-2",
        "/page-2?api_key=secret",
        "/page-2?cursor=opaque",
        "/page-2?q=" + "x" * 4100,
    ],
)
@respx.mock
def test_unsafe_or_unauditable_next_url_is_refused_before_second_dns(context: PluginContext, target: str) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=f'<a class="next" href="{target}">Next</a>')
    )
    transform = _transform("next_link_css")
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "error"
    assert context.landscape.count == 1


@respx.mock
def test_json_records_are_extracted_with_aligned_provenance_across_pages(context: PluginContext) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json", "link": '</page-2>; rel="next"'},
            json={"results": [{"name": "Agency One"}]},
        )
    )
    respx.get(f"https://{_IP}:443/page-2").mock(
        return_value=httpx.Response(200, headers={"content-type": "application/json"}, json={"results": [{"name": "Agency Two"}]})
    )
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "raw",
            "response_mode": "json",
            "records": {
                "field": "candidates",
                "provenance_field": "candidate_sources",
                "records_path": ["results"],
                "columns": [{"field": "name", "path": ["name"], "required": True}],
            },
            "pagination": {"mode": "link_header"},
            "http": {"abuse_contact": "test@example.com", "scraping_reason": "JSON test", "allowed_origins": ["https://example.com"]},
        }
    )
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "success"
    assert [record["name"] for record in result.row["candidates"]] == ["Agency One", "Agency Two"]
    assert [source["name"]["source_url"] for source in result.row["candidate_sources"]] == [
        "https://example.com/page-1",
        "https://example.com/page-2",
    ]
    assert result.row.contract.get_field("candidates").required
    assert result.row.contract.get_field("candidate_sources").required
    assert context.landscape.count == 2
    assert json.loads(result.row["content"]) == [
        {"results": [{"name": "Agency One"}]},
        {"results": [{"name": "Agency Two"}]},
    ]


@pytest.mark.parametrize(
    ("headers", "content"),
    [({"content-type": "text/html; charset=utf-8"}, b"\xff"), ({"content-type": "text/plain"}, b"second")],
)
@respx.mock
def test_response_policy_admits_every_pagination_page(context: PluginContext, headers: dict[str, str], content: bytes) -> None:
    respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html", "link": '</page-2>; rel="next"'}, text="first")
    )
    respx.get(f"https://{_IP}:443/page-2").mock(return_value=httpx.Response(200, headers=headers, content=content))
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "accepted_mime_types": ["text/html"],
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "pagination": {"mode": "link_header"},
            "http": {"abuse_contact": "test@example.com", "scraping_reason": "Response test", "allowed_origins": ["https://example.com"]},
        }
    )
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "error"
    assert context.landscape.count == 2


@pytest.mark.parametrize("mode", ["json", "xml"])
@pytest.mark.parametrize("page_count", [1, 2])
@respx.mock
def test_strict_paginated_documents_keep_valid_envelope_and_semantics(context: PluginContext, mode: str, page_count: int) -> None:
    from elspeth.contracts.plugin_semantics import ContentKind, SemanticValueType, TextFraming

    documents = (
        ['{"name":"One"}', '{"name":"Two"}']
        if mode == "json"
        else [
            '<?xml version="1.0"?><entry>One &amp; Two</entry>',
            "<entry>Three</entry>",
        ]
    )
    for page in range(page_count):
        headers = {"content-type": f"application/{mode}"}
        if page + 1 < page_count:
            headers["link"] = f'</page-{page + 2}>; rel="next"'
        respx.get(f"https://{_IP}:443/page-{page + 1}").mock(return_value=httpx.Response(200, headers=headers, text=documents[page]))
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "raw",
            "response_mode": mode,
            "pagination": {"mode": "link_header"},
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Strict pagination",
                "allowed_origins": ["https://example.com"],
            },
        }
    )
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1"}), context)
    assert result.status == "success"
    content = result.row["content"]
    if mode == "json":
        assert json.loads(content) == [json.loads(document) for document in documents[:page_count]]
    else:
        root = ElementTree.fromstring(content)
        assert root.tag == "pages"
        assert [page.text for page in root] == documents[:page_count]
    facts = transform.output_semantics().fields[0]
    assert facts.content_kind is ContentKind.UNKNOWN
    assert facts.value_type is SemanticValueType.STR
    assert facts.text_framing is TextFraming.UNCONSTRAINED


def test_link_header_merged_representation_must_fit_audit_budget() -> None:
    next_link = "<https://example.com/" + "a" * 2014 + '>; rel="next"'
    previous_link = "<https://example.com/" + "b" * 2014 + '>; rel="prev"'
    assert len(next_link) == len(previous_link) == 2048
    response = httpx.Response(200, headers=[("Link", next_link), ("Link", previous_link)])
    with pytest.raises(ValueError, match="exact replay evidence"):
        discover_next_url(response, "https://example.com/", PaginationConfig(mode="link_header"))
