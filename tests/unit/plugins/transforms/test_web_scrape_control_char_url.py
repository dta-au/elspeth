"""A row URL carrying an ASCII control character is a row error, never a crash.

Regression for sweep finding #2 (20261111-plugins-review): with the default
``http.allowed_origins`` (empty) the origin check returns before its
control-character refusal, and the SSRF validator admits such a URL
(``urlsplit`` drops ``\\n``/``\\t``; ``\\x00``/``\\x7f`` pass through). httpx then
raised ``httpx.InvalidURL`` -- which is neither ``ValueError`` nor
``httpx.RequestError`` -- out of ``process()``, crashing the run on Tier-3 row
data instead of returning ``TransformResult.error``.
"""

import socket
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import Call
from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.transforms.web_scrape import WebScrapeTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.mock_audit import mock_item_audit_authority

_IP = "104.18.27.120"

CONTROL_CHAR_URLS = [
    pytest.param("https://example.com/page\n", id="trailing-newline"),
    pytest.param("https://example.com/a\tb", id="tab"),
    pytest.param("https://example.com/\x00", id="nul-in-path"),
    pytest.param("https://example.com/\x7f", id="del-in-path"),
    pytest.param("https://exa\x00mple.com/", id="nul-in-host"),
    pytest.param("https://example.com/page\r", id="carriage-return"),
]
QUERIES = [pytest.param(None, id="no-query"), pytest.param({"q": "x"}, id="with-query")]


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


def _dns(_host: str, _port: int, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP, 0))]


def _transform(query: dict[str, str] | None, *, allowed_origins: list[str] | None = None) -> WebScrapeTransform:
    http: dict[str, object] = {"abuse_contact": "test@example.com", "scraping_reason": "Control-character URL unit test"}
    if allowed_origins is not None:
        http["allowed_origins"] = allowed_origins
    config: dict[str, object] = {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "content_field": "content",
        "fingerprint_field": "fingerprint",
        "format": "text",
        "http": http,
    }
    if query is not None:
        config["query"] = query
    return WebScrapeTransform(config)


def _process(transform: WebScrapeTransform, url: str, context: PluginContext) -> tuple[Any, BaseException | None, int]:
    with respx.mock:
        route = respx.route().mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>"))
        transform.on_start(context)
        result = None
        raised: BaseException | None = None
        try:
            with patch("socket.getaddrinfo", _dns):
                result = transform.process(make_pipeline_row({"url": url}), context)
        except Exception as exc:
            raised = exc
        return result, raised, route.call_count


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("url", CONTROL_CHAR_URLS)
def test_control_character_row_url_is_a_row_error_with_default_origins(
    context: PluginContext, url: str, query: dict[str, str] | None
) -> None:
    result, raised, calls = _process(_transform(query), url, context)

    assert raised is None, f"process() raised {type(raised).__module__}.{type(raised).__name__}: {raised}"
    assert result is not None
    assert result.status == "error"
    assert result.reason["reason"] == "validation_failed"
    assert result.reason["cause"] == "malformed_url"
    assert result.retryable is False
    assert calls == 0


@pytest.mark.parametrize("url", CONTROL_CHAR_URLS)
def test_control_character_row_url_refused_with_allowed_origins(context: PluginContext, url: str) -> None:
    """With allowed_origins configured the origin check refuses C0 controls, but
    it tests only ``ord(char) < 32``, so ``\\x7f`` (which httpx also rejects)
    escaped here too."""
    result, raised, calls = _process(_transform(None, allowed_origins=["https://example.com"]), url, context)

    assert raised is None
    assert result is not None
    assert result.status == "error"
    assert result.reason["reason"] == "validation_failed"
    assert calls == 0


@pytest.mark.parametrize("query", QUERIES)
def test_clean_row_url_still_fetches(context: PluginContext, query: dict[str, str] | None) -> None:
    """Negative control: the refusal must not catch an ordinary URL."""
    result, raised, calls = _process(_transform(query), "https://example.com/page", context)

    assert raised is None
    assert result is not None
    assert result.status == "success"
    assert calls == 1
