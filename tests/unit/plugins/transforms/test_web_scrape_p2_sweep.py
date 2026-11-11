"""Regression tests for the 20261111 plugins-review sweep findings on web_scrape."""

import socket
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import Call
from elspeth.contracts.call_mode import ReplaySSRFRequest
from elspeth.contracts.enums import RunMode
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.results import TransformResult
from elspeth.plugins.transforms.web_scrape import WebScrapeTransform
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


def _paginating_transform() -> WebScrapeTransform:
    return WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "text",
            "pagination": {"mode": "next_link_css", "next_link_selector": "a.next", "max_pages": 5},
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Pagination sweep regression",
                "allowed_origins": ["https://example.com"],
            },
        }
    )


# --- Finding #3: cycle detection must compare canonical URLs on both sides ---


@respx.mock
def test_self_link_with_urlencode_escaped_query_is_a_cycle_without_refetch(context: PluginContext) -> None:
    """A page linking to itself is a cycle even when urlencode re-escapes its query.

    httpx leaves ':' unescaped in a query; fingerprint_url re-encodes it as
    '%3A'. The seed URL and the discovered next URL must be compared in the
    same spelling, or page 1 is fetched and aggregated twice.
    """
    route = respx.get(f"https://{_IP}:443/page-1").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<p>first</p><a class="next" href="/page-1?sort=a:b">Next</a>',
        )
    )
    transform = _paginating_transform()
    transform.on_start(context)
    with patch("socket.getaddrinfo", _dns):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page-1?sort=a:b"}), context)

    assert result.status == "success"
    assert route.call_count == 1
    assert result.row["content"] == "first Next"
    metadata = result.success_reason["metadata"]
    assert metadata["pagination_stop_reason"] == "cycle"
    assert [page["number"] for page in metadata["pagination_pages"]] == [1]


# --- Finding #4: replay/verify refuse a malformed row URL exactly as live does ---


class _ArchiveMustNotBeConsulted:
    """A call-mode session whose archive must not be read for a refused URL."""

    def __init__(self, mode: RunMode) -> None:
        self.mode = mode

    def replay_ssrf_request(self, **_kwargs: Any) -> ReplaySSRFRequest:
        raise AssertionError("archived SSRF request consulted for a malformed URL")

    def preflight_verify_http_request(self, **_kwargs: Any) -> None:
        raise AssertionError("verify preflight reached for a malformed URL")


def _default_transform() -> WebScrapeTransform:
    return WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "http": {"abuse_contact": "test@example.com", "scraping_reason": "Replay sweep regression"},
        }
    )


@pytest.mark.parametrize("mode", [RunMode.LIVE, RunMode.REPLAY, RunMode.VERIFY])
def test_malformed_row_url_is_the_same_row_refusal_in_every_run_mode(context: PluginContext, mode: RunMode) -> None:
    transform = _default_transform()
    transform.on_start(context)
    if mode is not RunMode.LIVE:
        context.call_mode_session = _ArchiveMustNotBeConsulted(mode)
        context.run_mode = mode
        context.replay_from = "source-run"

    outcome: TransformResult | Exception
    try:
        outcome = transform.process(make_pipeline_row({"url": "http://[bad/x"}), context)
    except ValueError as exc:
        outcome = exc

    assert isinstance(outcome, TransformResult), f"{mode.value} raised {outcome!r} instead of refusing the row"
    assert outcome.status == "error"
    assert outcome.reason["reason"] == "validation_failed"
    assert outcome.reason["error_type"] == "SSRFBlockedError"
    assert outcome.reason["cause"] == "malformed_url"
