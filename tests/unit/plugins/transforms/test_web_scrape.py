"""Tests for WebScrapeTransform plugin.

All tests mock socket.getaddrinfo so that validate_url_for_ssrf() produces a
known resolved IP, then mock respx to match the IP-based URL that
get_ssrf_safe() actually sends.
"""

import base64
import hashlib
import os
import socket
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType, check_compatibility
from elspeth.contracts.audit import Call
from elspeth.contracts.call_data import HTTPCallRequest, MultipartPart, multipart_min_body_size
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.transforms.web_scrape import WebScrapeConfig, WebScrapeTransform
from elspeth.plugins.transforms.web_scrape_errors import (
    NetworkError,
    RateLimitError,
    ServerError,
)
from elspeth.testing import make_field, make_pipeline_row, make_row
from tests.fixtures.mock_audit import mock_item_audit_authority

# Stable test IP used for all DNS resolution mocks
_TEST_IP = "104.18.27.120"


class _ReturnValueCall:
    def __init__(self, return_value: Any) -> None:
        self.return_value = return_value
        self.call_count = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.call_count += 1
        return self.return_value


class _LandscapeRecorderFake:
    def __init__(self, call: Call) -> None:
        self.record_call = _ReturnValueCall(call)
        self.allocate_call_index = _ReturnValueCall(0)


class _PayloadStoreFake:
    def __init__(self, payload_ref: str) -> None:
        self.payload_ref = payload_ref
        self.stored_payloads: list[bytes] = []

    def store(self, payload: bytes) -> str:
        self.stored_payloads.append(payload)
        return self.payload_ref


class _RateLimitRegistryFake:
    def get_limiter(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _AuditedHTTPClientFake:
    def __init__(self, result: tuple[httpx.Response, str, Call]) -> None:
        self._result = result
        self.get_ssrf_safe_call_count = 0
        self.close_call_count = 0

    def get_ssrf_safe(self, *_args: Any, **_kwargs: Any) -> tuple[httpx.Response, str, Call]:
        self.get_ssrf_safe_call_count += 1
        return self._result

    def close(self) -> None:
        self.close_call_count += 1


def _mock_getaddrinfo(ip: str = _TEST_IP) -> Any:
    """Create a mock getaddrinfo that returns the given IP."""

    def _getaddrinfo(
        host: str,
        port: Any,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _getaddrinfo


@pytest.fixture
def mock_ctx():
    """Create PluginContext with required attributes for web scraping."""
    # Configure record_call to return a proper Call object so process() can
    # read call.request_ref and call.response_ref without FrameworkBugError.
    mock_call = Call(
        call_id="test-call-id",
        call_index=0,
        call_type=CallType.HTTP,
        status=CallStatus.SUCCESS,
        request_hash="test-request-hash",
        created_at=datetime.now(UTC),
        state_id="state-123",
        request_ref="test-request-ref-hash",
        response_hash="test-response-hash",
        response_ref="test-response-ref-hash",
        latency_ms=100.0,
    )
    landscape = _LandscapeRecorderFake(mock_call)
    payload_store = _PayloadStoreFake("test-processed-hash")
    rate_limit_registry = _RateLimitRegistryFake()

    # Create context
    ctx = PluginContext(
        run_id="test-run-456",
        **mock_item_audit_authority("test-run-456"),
        config={},
        landscape=landscape,
        payload_store=payload_store,
        rate_limit_registry=rate_limit_registry,
        state_id="state-123",
    )

    return ctx


def test_web_scrape_wires_on_success_and_on_error() -> None:
    """WebScrapeTransform routing is set via BaseTransform properties (bridge injection)."""
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Unit testing web scrape transform",
            },
        }
    )

    # Routing is injected by the instantiation bridge, not config
    assert transform.on_success is None
    assert transform.on_error is None

    # Verify bridge-style injection works
    transform.on_success = "output"
    transform.on_error = "errors"
    assert transform.on_success == "output"
    assert transform.on_error == "errors"


@respx.mock
def test_web_scrape_success_markdown(mock_ctx):
    """Successful scrape should enrich row with content and fingerprint."""
    html_content = "<html><body><h1>Title</h1><p>Content here</p></body></html>"

    # Mock the IP-based URL that get_ssrf_safe() sends
    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Unit testing web scrape transform",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success"
    assert "# Title" in result.row["page_content"]
    assert "Content here" in result.row["page_content"]
    assert result.row["page_fingerprint"] is not None
    assert len(result.row["page_fingerprint"]) == 64  # SHA-256
    assert result.row["fetch_status"] == 200


def _make_basic_transform_options() -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "content_field": "response_text",
        "fingerprint_field": "response_fingerprint",
        "format": "raw",
        "http": {
            "abuse_contact": "test@example.com",
            "scraping_reason": "Read-only public data retrieval",
        },
    }


def _make_post_transform(*, format: str = "raw", max_request_body_bytes: int = 1024) -> WebScrapeTransform:
    options = _make_basic_transform_options()
    options["format"] = format
    options["method"] = "POST"
    options["request_json_field"] = "query_body"
    options["http"]["max_request_body_bytes"] = max_request_body_bytes
    return WebScrapeTransform(options)


@pytest.mark.parametrize(
    ("scheme", "credential", "header_name", "expected_name", "expected_value"),
    [
        ("basic", "operator:password", None, "Authorization", "Basic " + base64.b64encode(b"operator:password").decode("ascii")),
        ("bearer", "token-123", None, "Authorization", "Bearer token-123"),
        ("api_key", "key-123", "X-Subscription", "X-Subscription", "key-123"),
    ],
)
def test_web_scrape_auth_header_builder_and_audit_fingerprint(
    scheme: str, credential: str, header_name: str | None, expected_name: str, expected_value: str
) -> None:
    from elspeth.plugins.infrastructure.clients.fingerprinting import fingerprint_headers
    from elspeth.plugins.transforms.web_scrape_auth import WebScrapeAuthConfig

    config = WebScrapeAuthConfig(scheme=scheme, origin="https://example.com", credential=credential, header_name=header_name)
    name, value = config.header()
    assert (name, value) == (expected_name, expected_value)
    safe_headers = fingerprint_headers({name: value}, force_fingerprint_names=frozenset({name.casefold()}))
    assert credential not in repr(config)
    assert credential not in repr(safe_headers)
    assert expected_value not in repr(safe_headers)
    assert "fingerprint:" in repr(safe_headers)


@pytest.mark.parametrize(
    "auth",
    [
        {"scheme": "bearer", "origin": "http://example.com", "credential": "token"},
        {"scheme": "bearer", "origin": "https://other.example", "credential": "token"},
        {"scheme": "api_key", "origin": "https://example.com", "credential": "key", "header_name": "Host"},
        {"scheme": "api_key", "origin": "https://example.com", "credential": "key", "header_name": "Cookie"},
        {"scheme": "api_key", "origin": "https://example.com", "credential": "key", "header_name": "Accept"},
    ],
)
def test_web_scrape_auth_config_refuses_unsafe_binding(auth: dict[str, str]) -> None:
    options = _make_basic_transform_options()
    options["url_field"] = None
    options["url"] = "https://example.com/page"
    options["http"]["allowed_origins"] = ["https://example.com"]
    options["auth"] = auth
    with pytest.raises(PluginConfigError):
        WebScrapeTransform(options)


def test_web_scrape_auth_secret_ref_preflight_and_literal_policy() -> None:
    from elspeth.core.secrets import (
        collect_credential_field_violations,
        collect_disallowed_secret_ref_markers,
        redact_secret_refs_for_validation,
    )

    options = _make_basic_transform_options()
    options["auth"] = {
        "scheme": "bearer",
        "origin": "https://example.com",
        "credential": {"secret_ref": "WEB_SEARCH_TOKEN"},
    }
    options["http"]["allowed_origins"] = ["https://example.com"]
    assert collect_credential_field_violations(options) == []
    assert collect_disallowed_secret_ref_markers(options) == []
    redacted = redact_secret_refs_for_validation({"options": options})["options"]
    WebScrapeTransform(redacted)
    options["auth"]["credential"] = "literal-token"
    assert collect_credential_field_violations(options) == ["credential"]


def test_web_scrape_basic_auth_accepts_printable_password_space() -> None:
    from elspeth.plugins.transforms.web_scrape_auth import WebScrapeAuthConfig

    auth = WebScrapeAuthConfig(scheme="basic", origin="https://example.com", credential="operator:pass phrase")
    assert auth.header() == ("Authorization", "Basic " + base64.b64encode(b"operator:pass phrase").decode("ascii"))


@respx.mock
def test_web_scrape_auth_remains_closed_until_response_evidence_is_secret_safe(mock_ctx) -> None:
    route = respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text="result"))
    options = _make_basic_transform_options()
    options["auth"] = {"scheme": "bearer", "origin": "https://example.com", "credential": "token"}
    options["http"]["allowed_origins"] = ["https://example.com"]
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)
    assert result.status == "error"
    assert result.reason["reason"] == "validation_failed"
    assert "response evidence is secret safe" in result.reason["error"]
    assert route.call_count == 0
    assert mock_ctx.landscape.record_call.call_count == 0


def test_web_scrape_auth_is_hidden_from_composer_catalog() -> None:
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.catalog.service import CatalogServiceImpl

    info = CatalogServiceImpl(get_shared_plugin_manager())._schema_cache[("transform", "web_scrape")]
    assert "auth" not in {field["name"] for field in info.knob_schema["fields"]}


@pytest.mark.parametrize("invalid_auth", [{"origin": "http://example.com"}, {"scheme": "api_key", "header_name": "Host"}])
def test_auth_validation_diagnostics_hide_resolved_credential(invalid_auth: dict[str, str]) -> None:
    from pydantic import ValidationError

    from elspeth.plugins.infrastructure.validation import validate_transform_config
    from elspeth.plugins.transforms.web_scrape_auth import WebScrapeAuthConfig

    credential = "CUSTODY42"
    # Keep the short credential last so Pydantic's abbreviated input rendering
    # cannot make the negative control pass by truncating the offending value.
    auth = {"scheme": "bearer", "origin": "https://x.test", **invalid_auth, "credential": credential}
    with pytest.raises(ValidationError) as standalone:
        WebScrapeAuthConfig.model_validate(auth)
    assert credential not in str(standalone.value)
    assert credential not in repr(standalone.value)
    assert credential not in repr(standalone.value.errors(include_input=False))

    options = _make_basic_transform_options()
    options["auth"] = auth
    options["http"]["allowed_origins"] = ["https://example.com"]
    with pytest.raises(ValidationError) as parent:
        WebScrapeConfig.model_validate(options)
    assert credential not in str(parent.value)
    assert credential not in repr(parent.value)
    with pytest.raises(PluginConfigError) as production:
        WebScrapeTransform(options)
    assert credential not in str(production.value)
    assert credential not in repr(production.value)
    assert credential not in str(production.value.__cause__)
    projected = validate_transform_config("web_scrape", options)
    assert projected
    assert credential not in repr(projected)
    assert all(error.value is None for error in projected)


def test_parent_auth_validation_error_hides_resolved_credential_on_unrelated_failure() -> None:
    from pydantic import ValidationError

    from elspeth.plugins.infrastructure.validation import validate_transform_config

    credential = "CUSTODY42"
    options = _make_basic_transform_options()
    options["auth"] = {"scheme": "bearer", "origin": "https://example.com", "credential": credential}
    options["http"]["allowed_origins"] = []
    with pytest.raises(ValidationError) as parent:
        WebScrapeConfig.model_validate(options)
    assert credential not in str(parent.value)
    assert credential not in repr(parent.value)
    with pytest.raises(PluginConfigError) as production:
        WebScrapeTransform(options)
    assert credential not in str(production.value)
    assert credential not in str(production.value.__cause__)
    projected = validate_transform_config("web_scrape", options)
    assert projected
    assert credential not in repr(projected)
    assert all(error.value is None for error in projected)


@pytest.mark.parametrize("schema", [None, {"mode": "invalid"}])
def test_wrapped_schema_diagnostics_omit_resolved_auth_input(schema: object) -> None:
    from elspeth.plugins.infrastructure.validation import validate_transform_config

    credential = "CUSTODY42"
    options = _make_basic_transform_options()
    options["schema"] = schema
    options["auth"] = {"scheme": "bearer", "origin": "https://example.com", "credential": credential}
    options["http"]["allowed_origins"] = ["https://example.com"]
    projected = validate_transform_config("web_scrape", options)
    assert projected
    assert credential not in repr(projected)
    assert all(error.value is None for error in projected)


def test_web_scrape_post_requires_body_field() -> None:
    options = _make_basic_transform_options()
    options["method"] = "POST"
    with pytest.raises(PluginConfigError, match="request_json_field"):
        WebScrapeTransform(options)


def test_web_scrape_post_declares_body_field_and_probes_it(mock_ctx) -> None:
    transform = _make_post_transform()
    assert "query_body" in transform.declared_input_fields
    probe_rows = transform.forward_invariant_probe_rows(make_pipeline_row({"unrelated": "value"}))
    probe = probe_rows[0]
    assert probe["query_body"] == {}
    result = transform.execute_forward_invariant_probe(probe_rows, mock_ctx)
    assert result.status == "success"
    assert result.row["unrelated"] == "value"


def test_web_scrape_post_rejects_body_field_created_by_transform() -> None:
    options = _make_basic_transform_options()
    options["method"] = "POST"
    options["request_json_field"] = "fetch_status"
    with pytest.raises(PluginConfigError, match="request_json_field names 'fetch_status'"):
        WebScrapeTransform(options)


def test_web_scrape_post_rejects_body_field_used_as_url() -> None:
    options = _make_basic_transform_options()
    options["method"] = "POST"
    options["request_json_field"] = "url"
    with pytest.raises(PluginConfigError, match="request_json_field and url_field must differ"):
        WebScrapeTransform(options)


def test_web_scrape_post_rejects_body_option_name_in_schema_columns() -> None:
    options = _make_basic_transform_options()
    options["method"] = "POST"
    options["request_json_field"] = "query_body"
    options["schema"] = {"mode": "observed", "guaranteed_fields": ["request_json_field"]}
    with pytest.raises(PluginConfigError, match="request_json_field"):
        WebScrapeTransform(options)


def test_web_scrape_get_rejects_body_field() -> None:
    options = _make_basic_transform_options()
    options["request_json_field"] = "query_body"
    with pytest.raises(PluginConfigError, match="request_json_field"):
        WebScrapeTransform(options)


@respx.mock
def test_web_scrape_multipart_sends_ordered_text_and_blob_parts(mock_ctx: PluginContext, tmp_path) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/upload").mock(
        return_value=httpx.Response(200, text="<main>received</main>", headers={"content-type": "text/html"})
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update({"url": "https://example.com/upload", "method": "POST", "request_multipart_field": "parts", "format": "raw"})
    options["http"]["max_request_body_bytes"] = 4096
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    store = FilesystemPayloadStore(base_path=tmp_path)
    blob = b"%PDF-1.7\r\ncontent"
    ref = store.store(blob)
    transform._payload_store = store
    row = make_pipeline_row(
        {
            "parts": [
                {"name": "q", "value": "A B"},
                {"name": "document", "blob_ref": ref, "filename": "form.pdf", "content_type": "application/pdf"},
                {"name": "q", "value": "Café"},
            ]
        }
    )

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(row, mock_ctx)

    assert result.status == "success"
    assert endpoint.call_count == 1
    request = endpoint.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=elspeth-")
    assert request.content.index(b"A B") < request.content.index(blob) < request.content.index("Café".encode())
    assert request.content.count(b'name="q"') == 2


def test_web_scrape_multipart_oversized_blob_refused_before_dns(mock_ctx: PluginContext, tmp_path) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update({"url": "https://example.com/upload", "method": "POST", "request_multipart_field": "parts"})
    options["http"]["max_request_body_bytes"] = 128
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    store = FilesystemPayloadStore(base_path=tmp_path)
    ref = store.store(b"x" * 1024)
    transform._payload_store = store

    with patch("socket.getaddrinfo") as resolve:
        result = transform.process(
            make_pipeline_row(
                {"parts": [{"name": "file", "blob_ref": ref, "filename": "data.bin", "content_type": "application/octet-stream"}]}
            ),
            mock_ctx,
        )

    resolve.assert_not_called()
    assert result.status == "error"
    assert result.reason is not None and "max_request_body_bytes" in str(result.reason)


def test_web_scrape_multipart_uses_one_aggregate_file_budget_before_dns(mock_ctx: PluginContext, tmp_path) -> None:
    first = b"a" * 64
    second = b"b" * 64
    first_ref = hashlib.sha256(first).hexdigest()
    second_ref = hashlib.sha256(second).hexdigest()
    parts = (
        MultipartPart(name="first", blob_ref=first_ref, filename="a.bin", content_type="application/octet-stream"),
        MultipartPart(name="second", blob_ref=second_ref, filename="b.bin", content_type="application/octet-stream"),
    )
    limit = multipart_min_body_size(parts, max_body_bytes=4096) + 100
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update({"url": "https://example.com/upload", "method": "POST", "request_multipart_field": "parts"})
    options["http"]["max_request_body_bytes"] = limit
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    store = FilesystemPayloadStore(base_path=tmp_path)
    assert store.store(first) == first_ref
    assert store.store(second) == second_ref
    transform._payload_store = store

    with patch.object(store, "retrieve_bounded", wraps=store.retrieve_bounded) as read, patch("socket.getaddrinfo") as resolve:
        result = transform.process(make_pipeline_row({"parts": [part.to_dict() for part in parts]}), mock_ctx)

    resolve.assert_not_called()
    assert [call.kwargs["max_bytes"] for call in read.call_args_list] == [100, 36]
    assert result.status == "error"
    assert result.reason is not None and "max_request_body_bytes" in str(result.reason)


@respx.mock
def test_web_scrape_post_sends_row_json_and_returns_raw_json(mock_ctx) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(
        return_value=httpx.Response(200, text='{"items":["found"]}', headers={"content-type": "application/json"})
    )
    transform = _make_post_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {"term": "found"}}), mock_ctx)

    assert result.status == "success"
    assert endpoint.call_count == 1
    assert endpoint.calls[0].request.method == "POST"
    assert endpoint.calls[0].request.headers["content-type"] == "application/json"
    assert endpoint.calls[0].request.content == b'{"term":"found"}'
    assert result.row["response_text"] == '{"items":["found"]}'
    assert result.row["fetch_status"] == 200


@respx.mock
def test_web_scrape_post_sends_ordered_urlencoded_form_to_fixed_url(mock_ctx: PluginContext) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="<main>Found</main>"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update(url="https://example.com/search", method="POST", request_form_field="search_form")
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(
            make_pipeline_row({"company_id": "1", "search_form": [{"name": "q", "value": "A B"}, {"name": "q", "value": "Café"}]}),
            mock_ctx,
        )

    assert result.status == "success"
    assert endpoint.call_count == 1
    request = endpoint.calls[0].request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded; charset=utf-8"
    assert request.content == b"q=A+B&q=Caf%C3%A9"
    assert result.row is not None
    assert result.row["company_id"] == "1"
    assert result.row["fetch_url_final"] == "https://example.com/search"


def test_web_scrape_post_rejects_malformed_form_before_dns(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.update(method="POST", request_form_field="search_form")
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(
            make_pipeline_row({"url": "https://example.com/search", "search_form": {"q": "secret-example"}}), mock_ctx
        )

    assert result.status == "error"
    assert result.retryable is False
    assert "secret-example" not in str(result.reason)


def test_web_scrape_rejects_multiple_post_body_sources() -> None:
    options = _make_basic_transform_options()
    options.update(method="POST", request_json_field="query_body", request_form_field="search_form")
    with pytest.raises(PluginConfigError, match=r"exactly one.*request_json_field.*request_form_field"):
        WebScrapeTransform(options)


@respx.mock
def test_web_scrape_post_invalid_body_refuses_before_dns_or_http(mock_ctx) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="ok"))
    transform = _make_post_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": ["secret-example"]}), mock_ctx)

    assert result.status == "error"
    assert result.retryable is False
    assert "secret-example" not in str(result.reason)
    assert endpoint.call_count == 0


def test_web_scrape_post_missing_row_body_refuses_before_dns(mock_ctx) -> None:
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search"}), mock_ctx)
    assert result.status == "error"
    assert result.retryable is False
    assert result.reason["reason"] == "validation_failed"


@respx.mock
def test_web_scrape_post_oversized_body_refuses_before_dns_or_http(mock_ctx) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="ok"))
    transform = _make_post_transform(max_request_body_bytes=16)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {"term": "too long"}}), mock_ctx)

    assert result.status == "error"
    assert result.retryable is False
    assert endpoint.call_count == 0


@pytest.mark.parametrize("body", [{"items": [float("nan")]}, {1: "not a string key"}, {"item": b"not JSON"}])
def test_web_scrape_post_rejects_non_json_values_before_dns(mock_ctx, body: object) -> None:
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not run")):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": body}), mock_ctx)
    assert result.status == "error"
    assert result.retryable is False
    assert "not a string key" not in str(result.reason)


@respx.mock
def test_web_scrape_post_redirect_is_not_followed(mock_ctx) -> None:
    first = respx.post(f"https://{_TEST_IP}:443/search").mock(
        return_value=httpx.Response(307, headers={"location": "http://127.0.0.1/admin"})
    )
    next_hop = respx.get("http://127.0.0.1/admin").mock(return_value=httpx.Response(200, text="wrong"))
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {}}), mock_ctx)
    assert result.status == "error"
    assert result.retryable is False
    assert first.call_count == 1
    assert next_hop.call_count == 0


@pytest.mark.parametrize("status", [408, 429, 503])
@respx.mock
def test_web_scrape_post_server_error_is_not_retried(mock_ctx, status: int) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(status))
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {}}), mock_ctx)
    assert result.status == "error"
    assert result.retryable is False
    assert endpoint.call_count == 1


@respx.mock
def test_web_scrape_post_connection_failure_is_not_retried_or_leaked(mock_ctx) -> None:
    endpoint = respx.post(f"https://{_TEST_IP}:443/search").mock(side_effect=httpx.ConnectError("remote unavailable"))
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(
            make_pipeline_row({"url": "https://example.com/search", "query_body": {"token": "secret-example"}}), mock_ctx
        )
    assert result.status == "error"
    assert result.retryable is False
    assert endpoint.call_count == 1
    assert "secret-example" not in str(result.reason)


@respx.mock
def test_web_scrape_post_html_response_uses_existing_extraction(mock_ctx) -> None:
    respx.post(f"https://{_TEST_IP}:443/search").mock(
        return_value=httpx.Response(200, text="<html><body><h1>Found</h1></body></html>", headers={"content-type": "text/html"})
    )
    transform = _make_post_transform(format="markdown")
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {"q": "Found"}}), mock_ctx)
    assert result.status == "success"
    assert "# Found" in result.row["response_text"]


@respx.mock
def test_web_scrape_post_json_requires_raw_format(mock_ctx) -> None:
    respx.post(f"https://{_TEST_IP}:443/search").mock(
        return_value=httpx.Response(200, text='{"items":[]}', headers={"content-type": "application/json"})
    )
    transform = _make_post_transform(format="text")
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {}}), mock_ctx)
    assert result.status == "error"
    assert result.reason["reason"] == "non_text_content_type"


@respx.mock
def test_web_scrape_strict_json_mode_rejects_invalid_document_before_fingerprint(mock_ctx) -> None:
    respx.get(f"https://{_TEST_IP}:443/data").mock(
        return_value=httpx.Response(200, content=b'{"value":NaN}', headers={"content-type": "application/json"})
    )
    options = _make_basic_transform_options()
    options["response_mode"] = "json"
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/data"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "invalid_json"
    assert result.row is None


@respx.mock
def test_web_scrape_json_records_refuse_invalid_unicode_before_fingerprint(mock_ctx) -> None:
    respx.get(f"https://{_TEST_IP}:443/data").mock(
        return_value=httpx.Response(200, content=b'[{"x":"\\ud800"}]', headers={"content-type": "application/json"})
    )
    options = _make_basic_transform_options()
    options["response_mode"] = "json"
    options["records"] = {"field": "records", "columns": [{"field": "x", "path": ["x"]}]}
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/data"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "content_extraction_failed"
    assert result.reason["error"] == "JSON record column contains an invalid Unicode scalar"
    assert result.row is None


@respx.mock
def test_web_scrape_xml_mode_accepts_well_formed_document(mock_ctx) -> None:
    xml = b"<results><name>Example</name></results>"
    respx.get(f"https://{_TEST_IP}:443/data").mock(
        return_value=httpx.Response(200, content=xml, headers={"content-type": "application/xml; charset=utf-8"})
    )
    options = _make_basic_transform_options()
    options["response_mode"] = "xml"
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/data"}), mock_ctx)

    assert result.status == "success"
    assert result.row["response_text"] == xml.decode()


@respx.mock
def test_web_scrape_post_binary_response_is_rejected(mock_ctx) -> None:
    respx.post(f"https://{_TEST_IP}:443/search").mock(
        return_value=httpx.Response(200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"})
    )
    transform = _make_post_transform()
    transform.on_start(mock_ctx)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/search", "query_body": {}}), mock_ctx)
    assert result.status == "error"
    assert result.retryable is False
    assert result.reason["reason"] == "non_text_content_type"


@respx.mock
def test_web_scrape_404_returns_error(mock_ctx):
    """404 should return error result (non-retryable)."""
    respx.get(f"https://{_TEST_IP}:443/missing").mock(return_value=httpx.Response(404))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/missing"}), mock_ctx)

    assert result.status == "error"
    assert "NotFoundError" in result.reason["error_type"]
    assert "404" in result.reason["error"]


@respx.mock
def test_web_scrape_500_raises_for_retry(mock_ctx):
    """HTTP 500 should raise ServerError (retryable)."""
    respx.get(f"https://{_TEST_IP}:443/error").mock(return_value=httpx.Response(500))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(ServerError) as exc_info:
        transform.process(make_pipeline_row({"url": "https://example.com/error"}), mock_ctx)

    assert exc_info.value.retryable is True
    assert "500" in str(exc_info.value)


@respx.mock
def test_web_scrape_429_raises_for_retry(mock_ctx):
    """HTTP 429 should raise RateLimitError (retryable)."""
    respx.get(f"https://{_TEST_IP}:443/throttled").mock(return_value=httpx.Response(429))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(RateLimitError) as exc_info:
        transform.process(make_pipeline_row({"url": "https://example.com/throttled"}), mock_ctx)

    assert exc_info.value.retryable is True
    assert "429" in str(exc_info.value)


def test_web_scrape_invalid_scheme_returns_error(mock_ctx):
    """Non-HTTP scheme should return error (security violation)."""
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    result = transform.process(make_pipeline_row({"url": "ftp://example.com/file"}), mock_ctx)

    assert result.status == "error"
    assert "SSRFBlockedError" in result.reason["error_type"]
    assert "scheme" in result.reason["error"].lower()


def test_web_scrape_private_ip_returns_error(mock_ctx):
    """Private IP should return error (SSRF prevention)."""
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254")):
        result = transform.process(make_pipeline_row({"url": "http://169.254.169.254/metadata"}), mock_ctx)

    assert result.status == "error"
    assert "SSRFBlockedError" in result.reason["error_type"]


@respx.mock
def test_web_scrape_text_format(mock_ctx):
    """Test text extraction format."""
    html_content = "<html><body><h1>Title</h1><p>Content here</p></body></html>"

    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "text",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success"
    # Text format should not include markdown
    assert result.row["page_content"] == "Title Content here"
    assert "#" not in result.row["page_content"]
    assert "Title" in result.row["page_content"]
    assert "Content here" in result.row["page_content"]


@respx.mock
def test_web_scrape_text_format_uses_configured_separator(mock_ctx):
    """Text extraction can preserve line boundaries before line_explode consumes it."""
    html_content = "<html><body><h1>Title</h1><p>Content here</p><ul><li>One</li><li>Two</li></ul></body></html>"

    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "text",
            "text_separator": "\n",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success"
    assert result.row["page_content"] == "Title\nContent here\nOne\nTwo"


@respx.mock
def test_web_scrape_strips_script_tags(mock_ctx):
    """Test that script tags are stripped by default."""
    html_content = """
    <html>
        <body>
            <h1>Title</h1>
            <script>alert('malicious');</script>
            <p>Content here</p>
        </body>
    </html>
    """

    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "text",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success"
    assert "alert" not in result.row["page_content"]
    assert "malicious" not in result.row["page_content"]
    assert "Title" in result.row["page_content"]


@respx.mock
def test_web_scrape_payload_storage(mock_ctx):
    """Test that payload hashes are stored in success_reason metadata (not in row)."""
    html_content = "<html><body><h1>Title</h1></body></html>"

    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success"
    # Hash fields are now audit-only — they live in success_reason["metadata"], not in the row
    assert "fetch_request_hash" not in result.row
    assert "fetch_response_raw_hash" not in result.row
    assert "fetch_response_processed_hash" not in result.row
    # Verify hashes are in success_reason["metadata"]
    metadata = result.success_reason["metadata"]
    assert metadata["fetch_request_hash"] == "test-request-ref-hash"
    assert metadata["fetch_response_raw_hash"] == "test-response-ref-hash"
    assert metadata["fetch_response_processed_hash"] == "test-processed-hash"


@respx.mock
def test_web_scrape_framework_bug_error_when_call_refs_none(mock_ctx):
    """FrameworkBugError raised when Call.request_ref or response_ref is None.

    This guards against a misconfigured PayloadStore (no payload_store),
    which would produce Calls with None refs and silently lose audit provenance.
    """
    from elspeth.contracts.errors import FrameworkBugError

    # Override the fixture's mock_call to return None refs
    mock_call_no_refs = Call(
        call_id="test-call-id",
        call_index=0,
        call_type=CallType.HTTP,
        status=CallStatus.SUCCESS,
        request_hash="test-request-hash",
        created_at=datetime.now(UTC),
        state_id="state-123",
        request_ref=None,
        response_hash="test-response-hash",
        response_ref=None,
        latency_ms=100.0,
    )
    mock_ctx.landscape.record_call.return_value = mock_call_no_refs

    html_content = "<html><body><h1>Title</h1></body></html>"
    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(FrameworkBugError, match="request_ref/response_ref"):
        transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)


@respx.mock
def test_web_scrape_timeout_raises_network_error(mock_ctx):
    """Timeout should raise NetworkError (retryable)."""
    # Mock timeout by raising httpx.TimeoutException on the IP-based URL
    respx.get(f"https://{_TEST_IP}:443/slow").mock(side_effect=httpx.TimeoutException("Connection timeout"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
                "timeout": 1,
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(NetworkError) as exc_info:
        transform.process(make_pipeline_row({"url": "https://example.com/slow"}), mock_ctx)

    assert exc_info.value.retryable is True
    assert "timeout" in str(exc_info.value).lower()


@respx.mock
def test_web_scrape_connection_error_raises_network_error(mock_ctx):
    """Connection error should raise NetworkError (retryable)."""
    respx.get(f"https://{_TEST_IP}:443/unreachable").mock(side_effect=httpx.ConnectError("Connection refused"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(NetworkError) as exc_info:
        transform.process(make_pipeline_row({"url": "https://example.com/unreachable"}), mock_ctx)

    assert exc_info.value.retryable is True
    assert "connection" in str(exc_info.value).lower()


@respx.mock
def test_web_scrape_403_returns_error(mock_ctx):
    """HTTP 403 should return error result (non-retryable)."""
    respx.get(f"https://{_TEST_IP}:443/forbidden").mock(return_value=httpx.Response(403))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/forbidden"}), mock_ctx)

    assert result.status == "error"
    assert "ForbiddenError" in result.reason["error_type"]
    assert "403" in result.reason["error"]


@respx.mock
def test_web_scrape_401_returns_error(mock_ctx):
    """HTTP 401 should return error result (non-retryable)."""
    respx.get(f"https://{_TEST_IP}:443/unauthorized").mock(return_value=httpx.Response(401))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/unauthorized"}), mock_ctx)

    assert result.status == "error"
    assert "UnauthorizedError" in result.reason["error_type"]
    assert "401" in result.reason["error"]


@respx.mock
def test_web_scrape_with_pipeline_row(mock_ctx):
    """Test that web_scrape works correctly with PipelineRow input."""
    html_content = "<html><body><h1>Test</h1></body></html>"
    respx.get(f"https://{_TEST_IP}:443/test").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing PipelineRow compatibility",
            },
        }
    )
    transform.on_start(mock_ctx)

    # Create PipelineRow input (simulates what engine passes to transforms)
    fields = (make_field("url", str, original_name="url", required=True, source="declared"),)
    contract = SchemaContract(mode="FIXED", fields=fields, locked=True)
    pipeline_row = make_row({"url": "https://example.com/test"}, contract=contract)

    # Process should work with PipelineRow (uses row.to_dict() internally)
    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(pipeline_row, mock_ctx)

    assert result.status == "success"
    assert "# Test" in result.row["page_content"]
    assert result.row["page_fingerprint"] is not None
    assert result.row["fetch_status"] == 200


@respx.mock
def test_web_scrape_output_contract_matches_declared_enriched_fields(mock_ctx):
    """Declared web_scrape fields must keep declared metadata at emission."""
    html_content = "<html><body><h1>Title</h1></body></html>"
    respx.get(f"https://{_TEST_IP}:443/page").mock(return_value=httpx.Response(200, text=html_content))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing declared output contract metadata",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    field_by_name = {field.normalized_name: field for field in result.row.contract.fields}
    for field_name in (
        "page_content",
        "page_fingerprint",
        "fetch_status",
        "fetch_url_final",
        "fetch_url_final_ip",
    ):
        assert field_by_name[field_name].required is True
        assert field_by_name[field_name].source == "declared"


@respx.mock
def test_web_scrape_follows_redirects_301(mock_ctx):
    """HTTP 301 redirect should be followed and final URL recorded.

    Edge case: 301 Moved Permanently redirects are common for URL migrations.
    The scraper should follow the redirect and record both the requested URL
    and the final URL after redirect resolution. Both the logical hostname URL
    and the IP-based connection URL are recorded for audit comparison.
    """
    respx.get(f"https://{_TEST_IP}:443/old").mock(return_value=httpx.Response(301, headers={"Location": "https://example.com/new"}))
    respx.get(f"https://{_TEST_IP}:443/new").mock(return_value=httpx.Response(200, text="<html><body><h1>New Location</h1></body></html>"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing redirect handling",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/old"}), mock_ctx)

    assert result.status == "success"
    assert "# New Location" in result.row["page_content"]
    assert result.row["fetch_status"] == 200
    assert result.row["fetch_url_final"] == "https://example.com/new"
    assert result.row["fetch_url_final_ip"] == _TEST_IP


@respx.mock
def test_web_scrape_follows_redirect_chain(mock_ctx):
    """Multiple redirects (301->302->200) should be followed to final destination."""
    respx.get(f"https://{_TEST_IP}:443/start").mock(return_value=httpx.Response(301, headers={"Location": "https://example.com/middle"}))
    respx.get(f"https://{_TEST_IP}:443/middle").mock(return_value=httpx.Response(302, headers={"Location": "https://example.com/end"}))
    respx.get(f"https://{_TEST_IP}:443/end").mock(
        return_value=httpx.Response(200, text="<html><body><h1>Final Destination</h1></body></html>")
    )

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing redirect chain",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/start"}), mock_ctx)

    assert result.status == "success"
    assert "# Final Destination" in result.row["page_content"]
    assert result.row["fetch_status"] == 200
    assert result.row["fetch_url_final"] == "https://example.com/end"
    assert result.row["fetch_url_final_ip"] == _TEST_IP


@respx.mock
def test_web_scrape_redirect_limit_exceeded(mock_ctx):
    """Excessive redirects should return non-retryable error result.

    A redirect loop is a configuration problem, not a transient failure.
    The TooManyRedirects exception from httpx is caught by _fetch_url and
    re-raised as InvalidURLError (non-retryable), which process() converts
    to a TransformResult.error() for row quarantine.
    """
    respx.get(f"https://{_TEST_IP}:443/a").mock(return_value=httpx.Response(301, headers={"Location": "https://example.com/b"}))
    respx.get(f"https://{_TEST_IP}:443/b").mock(return_value=httpx.Response(301, headers={"Location": "https://example.com/a"}))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing redirect limit",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/a"}), mock_ctx)

    assert result.status == "error"
    assert "InvalidURLError" in result.reason["error_type"]
    assert "redirect" in result.reason["error"].lower()


@respx.mock
def test_web_scrape_malformed_html_graceful_degradation(mock_ctx):
    """Malformed HTML should still extract text content without crashing."""
    malformed_html = """
    <html>
    <body>
        <h1>Title
        <p>Paragraph without closing tag
        <div>
            <p>Nested paragraph
        </div>
        <!-- Missing closing tags for body and html -->
    """

    respx.get(f"https://{_TEST_IP}:443/malformed").mock(return_value=httpx.Response(200, text=malformed_html))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing malformed HTML",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/malformed"}), mock_ctx)

    # Should succeed despite malformed HTML
    assert result.status == "success"
    # Content should be extracted (BeautifulSoup auto-closes tags)
    assert "Title" in result.row["page_content"]
    assert "Paragraph without closing tag" in result.row["page_content"]
    assert "Nested paragraph" in result.row["page_content"]
    assert result.row["fetch_status"] == 200


@respx.mock
def test_web_scrape_extract_content_exception_returns_error(mock_ctx):
    """When extract_content() raises, process() returns TransformResult.error() instead of crashing.

    This is the Tier 3 boundary protection: response.text is external data and parsing it
    can fail in ways we can't predict. The pipeline must quarantine the row, not crash.
    """
    respx.get(f"https://{_TEST_IP}:443/bad").mock(return_value=httpx.Response(200, text="<html>valid</html>"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing parse error",
            },
        }
    )
    transform.on_start(mock_ctx)

    # Simulate extract_content() raising an unexpected exception
    with (
        patch("socket.getaddrinfo", _mock_getaddrinfo()),
        patch(
            "elspeth.plugins.transforms.web_scrape.extract_content",
            side_effect=RuntimeError("html2text internal error"),
        ),
    ):
        result = transform.process(make_pipeline_row({"url": "https://example.com/bad"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "content_extraction_failed"
    assert "html2text internal error" in result.reason["error"]
    assert result.reason["error_type"] == "RuntimeError"
    # The URL is row data: the reason never names it (C3).
    assert "url" not in result.reason
    assert "example.com" not in repr(result.reason)


@respx.mock
def test_web_scrape_extract_content_tier3_valueerror_returns_error(mock_ctx):
    """Tier 3 boundary: ValueError from extract_content (wrapping library exceptions) returns error.

    extract_content() catches AttributeError/TypeError from BeautifulSoup/html2text
    on malformed HTML and re-raises as ValueError. The caller catches ValueError
    and returns TransformResult.error() — pipeline continues, row is quarantined.
    """
    respx.get(f"https://{_TEST_IP}:443/malformed").mock(return_value=httpx.Response(200, text="<html>malformed</html>"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing Tier 3 parse error",
            },
        }
    )
    transform.on_start(mock_ctx)

    with (
        patch("socket.getaddrinfo", _mock_getaddrinfo()),
        patch(
            "elspeth.plugins.transforms.web_scrape.extract_content",
            side_effect=ValueError("HTML extraction failed on malformed content: NoneType"),
        ),
    ):
        result = transform.process(make_pipeline_row({"url": "https://example.com/malformed"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "content_extraction_failed"
    assert result.reason["error_type"] == "ValueError"


@respx.mock
def test_web_scrape_binary_response_does_not_crash(mock_ctx):
    """Binary content in response.text should be handled gracefully.

    When a server returns binary data (image, PDF) with a text content-type,
    httpx will attempt UTF-8 decoding. If extraction fails on the result,
    we should get an error result, not a pipeline crash.
    """
    # Binary data that httpx will lossy-decode to replacement characters
    binary_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\xff\xfe" * 100
    respx.get(f"https://{_TEST_IP}:443/binary").mock(
        return_value=httpx.Response(200, content=binary_content, headers={"content-type": "text/html"})
    )

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing binary response",
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/binary"}), mock_ctx)

    # Should either succeed (BeautifulSoup is very forgiving) or return error — never crash
    assert result.status in ("success", "error")


@respx.mock
def test_web_scrape_unicode_decode_error_returns_error(mock_ctx):
    """UnicodeDecodeError during extraction returns error result."""
    respx.get(f"https://{_TEST_IP}:443/encoding").mock(return_value=httpx.Response(200, text="<html>ok</html>"))

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing encoding error",
            },
        }
    )
    transform.on_start(mock_ctx)

    with (
        patch("socket.getaddrinfo", _mock_getaddrinfo()),
        patch(
            "elspeth.plugins.transforms.web_scrape.extract_content",
            side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ),
    ):
        result = transform.process(make_pipeline_row({"url": "https://example.com/encoding"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "content_extraction_failed"
    assert result.reason["error_type"] == "UnicodeDecodeError"


# --- Config validation tests (WebScrapeHTTPConfig sub-model) ---


def _base_config(**overrides: Any) -> dict[str, Any]:
    """Build a valid WebScrapeTransform config dict, with overrides applied."""
    cfg: dict[str, Any] = {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "content_field": "page_content",
        "fingerprint_field": "page_fingerprint",
        "http": {
            "abuse_contact": "test@example.com",
            "scraping_reason": "Testing",
        },
    }
    cfg.update(overrides)
    return cfg


def test_http_config_missing_abuse_contact_raises() -> None:
    """Missing required abuse_contact must raise PluginConfigError."""
    with pytest.raises(PluginConfigError, match="abuse_contact"):
        WebScrapeTransform(_base_config(http={"scraping_reason": "Testing"}))


def test_http_config_missing_scraping_reason_raises() -> None:
    """Missing required scraping_reason must raise PluginConfigError."""
    with pytest.raises(PluginConfigError, match="scraping_reason"):
        WebScrapeTransform(_base_config(http={"abuse_contact": "test@example.com"}))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("abuse_contact", "<OPERATOR_REQUIRED>"),
        ("abuse_contact", "operator required"),
        ("scraping_reason", "<OPERATOR_REQUIRED>"),
        ("scraping_reason", "operator required"),
    ],
)
def test_http_config_rejects_operator_required_placeholders(field_name: str, value: str) -> None:
    """Wire-visible HTTP identity fields must not accept placeholder sentinels."""
    http = {
        "abuse_contact": "ops@somecompany.gov.au",
        "scraping_reason": "User-authorised page colour lookup",
    }
    http[field_name] = value

    with pytest.raises(PluginConfigError, match=field_name):
        WebScrapeTransform(_base_config(http=http))


@pytest.mark.parametrize("field_name", ["abuse_contact", "scraping_reason"])
def test_http_config_rejects_non_ascii_header_values(field_name: str) -> None:
    """Wire-visible HTTP identity fields must be ASCII-encodable.

    ``abuse_contact`` and ``scraping_reason`` are sent verbatim as the
    ``X-Abuse-Contact`` / ``X-Scraping-Reason`` request headers. HTTP header
    values must be ASCII-encodable; a typographic em dash (U+2014) — exactly
    what the guided LLM composer routinely emits — otherwise raises
    ``UnicodeEncodeError`` deep inside the HTTP client mid-request, escaping
    ``on_error`` row handling and aborting the entire run. Reject it at config
    validation so it surfaces as a PluginConfigError before any fetch starts.
    """
    http = {
        "abuse_contact": "ops@somecompany.gov.au",
        "scraping_reason": "User-authorised page colour lookup",
    }
    http[field_name] = "Demo pipeline — fetching own demo pages"

    with pytest.raises(PluginConfigError) as exc_info:
        WebScrapeTransform(_base_config(http=http))
    message = str(exc_info.value)
    assert field_name in message
    assert "ASCII" in message


def test_http_config_extra_field_raises() -> None:
    """Unknown fields in http config must be rejected (extra=forbid)."""
    with pytest.raises(PluginConfigError, match="extra_field"):
        WebScrapeTransform(
            _base_config(
                http={
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing",
                    "extra_field": "should fail",
                }
            )
        )


def test_http_config_timeout_zero_raises() -> None:
    """Timeout must be > 0."""
    with pytest.raises(PluginConfigError, match="timeout"):
        WebScrapeTransform(
            _base_config(
                http={
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing",
                    "timeout": 0,
                }
            )
        )


def test_http_config_timeout_negative_raises() -> None:
    """Negative timeout must be rejected."""
    with pytest.raises(PluginConfigError, match="timeout"):
        WebScrapeTransform(
            _base_config(
                http={
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing",
                    "timeout": -5,
                }
            )
        )


def test_http_config_timeout_default() -> None:
    """Timeout defaults to 30 when not specified."""
    transform = WebScrapeTransform(_base_config())
    assert transform._timeout == 30


def test_http_config_timeout_custom() -> None:
    """Custom timeout value is respected."""
    transform = WebScrapeTransform(
        _base_config(
            http={
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing",
                "timeout": 60,
            }
        )
    )
    assert transform._timeout == 60


def test_web_scrape_forward_probe_preserves_baseline_and_restores_payload_store(mock_ctx) -> None:
    """Invariant probe should use a hermetic fetch seam and restore injected state."""
    transform = WebScrapeTransform(WebScrapeTransform.probe_config())

    assert WebScrapeTransform.passes_through_input is True

    original_fetch = transform._fetch_url
    original_payload_store = _PayloadStoreFake("existing-hash")
    transform._payload_store = original_payload_store

    base_row = make_pipeline_row({"baseline": "kept"})
    result = transform.execute_forward_invariant_probe(
        transform.forward_invariant_probe_rows(base_row),
        mock_ctx,
    )

    assert result.status == "success"
    assert result.row is not None
    assert result.row["baseline"] == "kept"
    assert result.row["page_content"]
    assert result.row["page_fingerprint"]
    assert result.row["fetch_status"] == 200
    assert transform._payload_store is original_payload_store
    assert transform._fetch_url == original_fetch


class TestWebScrapeDeclaredOutputFields:
    """Tests for declared_output_fields — centralized collision detection support.

    Field collision detection is enforced centrally by TransformExecutor
    (see TestTransformExecutor in test_executors.py). These tests verify
    that WebScrapeTransform correctly declares its output fields so the
    executor can perform pre-execution collision checks.
    """

    def test_declared_output_fields_contains_hardcoded_fields(self):
        """declared_output_fields includes hardcoded fetch_* operational fields.

        Hash fields are audit-only (in success_reason["metadata"]) — not in declared_output_fields.
        """
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing declared fields",
                },
            }
        )

        assert "fetch_status" in transform.declared_output_fields
        assert "fetch_url_final" in transform.declared_output_fields
        assert "fetch_url_final_ip" in transform.declared_output_fields
        # Hash fields are audit-only — not declared as output fields
        assert "fetch_request_hash" not in transform.declared_output_fields
        assert "fetch_response_raw_hash" not in transform.declared_output_fields
        assert "fetch_response_processed_hash" not in transform.declared_output_fields

    def test_declared_output_fields_contains_configurable_fields(self):
        """declared_output_fields includes configurable content and fingerprint fields."""
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing declared fields",
                },
            }
        )

        assert "page_content" in transform.declared_output_fields
        assert "page_fingerprint" in transform.declared_output_fields

    def test_declared_output_fields_adapts_to_config(self):
        """declared_output_fields changes when content/fingerprint field names change."""
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "scraped_html",
                "fingerprint_field": "content_hash",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing declared fields",
                },
            }
        )

        assert "scraped_html" in transform.declared_output_fields
        assert "content_hash" in transform.declared_output_fields
        # Old names should NOT be present
        assert "page_content" not in transform.declared_output_fields
        assert "page_fingerprint" not in transform.declared_output_fields

    def test_declared_output_fields_drives_schema_evolution(self):
        """declared_output_fields is non-empty, enabling schema evolution."""
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Testing declared fields",
                },
            }
        )

        assert transform.declared_output_fields


class TestWebScrapeDeclaredInputFields:
    """Tests for declared_input_fields — static external-call preconditions."""

    def test_url_field_is_declared_as_static_input_requirement(self) -> None:
        transform = WebScrapeTransform(_base_config())

        assert transform.declared_input_fields == frozenset({"url"})

    def test_url_field_is_merged_with_explicit_required_input_fields(self) -> None:
        transform = WebScrapeTransform(_base_config(required_input_fields=["tenant_id"]))

        assert transform.declared_input_fields == frozenset({"url", "tenant_id"})


@respx.mock
def test_fixed_url_search_uses_row_data_without_url_column(mock_ctx: PluginContext) -> None:
    fixed_url = "https://example.com/search"
    route = respx.get(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="<main>Found</main>"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = fixed_url
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"company_query": "Acme"}), mock_ctx)

    assert transform.declared_input_fields == frozenset()
    assert route.call_count == 1
    assert result.status == "success"
    assert result.row is not None
    assert result.row["company_query"] == "Acme"
    assert result.row["fetch_url_final"] == fixed_url


@respx.mock
def test_fixed_url_search_maps_row_field_to_query_parameter(mock_ctx: PluginContext) -> None:
    route = respx.get(f"https://{_TEST_IP}:443/Search/ResultsActive?SearchText=ACME+%26+Co").mock(
        return_value=httpx.Response(200, text="<main>Matching names</main>")
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/Search/ResultsActive"
    options["query_fields"] = {"SearchText": "company_query"}
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"company_query": "ACME & Co"}), mock_ctx)

    assert transform.declared_input_fields == frozenset({"company_query"})
    assert route.call_count == 1
    assert result.status == "success"
    assert result.row is not None
    assert result.row["fetch_url_final"] == "https://example.com/Search/ResultsActive?SearchText=ACME+%26+Co"


@respx.mock
def test_fixed_site_search_extracts_bounded_candidate_records(mock_ctx: PluginContext) -> None:
    respx.get(f"https://{_TEST_IP}:443/directory?q=Shared+Name").mock(
        return_value=httpx.Response(
            200,
            text='<main><ul><li><a href="/one" class="office primary">Agency One</a><span class="state">ACT</span></li>'
            '<li><a href="/two">Agency Two</a></li></ul></main>',
            headers={"content-type": "text/html"},
        )
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/directory"
    options["query_fields"] = {"q": "search_text"}
    options["records"] = {
        "field": "candidates",
        "selector": "main li",
        "max_records": 10,
        "columns": [
            {"field": "name", "selector": "a", "required": True},
            {"field": "detail_path", "selector": "a", "attribute": "href", "required": True},
            {"field": "tags", "selector": "a", "attribute": "class"},
            {"field": "state", "selector": ".state"},
        ],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"search_text": "Shared Name"}), mock_ctx)

    assert result.status == "success"
    assert result.row is not None
    assert result.row.to_dict()["candidates"] == [
        {"name": "Agency One", "detail_path": "/one", "tags": "office primary", "state": "ACT"},
        {"name": "Agency Two", "detail_path": "/two", "tags": None, "state": None},
    ]
    assert "candidates" in transform.declared_output_fields


@respx.mock
def test_structured_records_emit_separate_sanitized_provenance(mock_ctx: PluginContext) -> None:
    respx.get(f"https://{_TEST_IP}:443/directory").mock(
        return_value=httpx.Response(
            200,
            text='<main><a href="/one">Agency One</a><a href="/two">Agency Two</a></main>',
            headers={"content-type": "text/html"},
        )
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/directory"
    options["records"] = {
        "field": "candidates",
        "provenance_field": "candidate_sources",
        "selector": "main",
        "columns": [{"field": "links", "selector": "a", "attribute": "href", "multiple": "all", "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    assert {"candidates", "candidate_sources"} <= transform.declared_output_fields
    assert transform._output_schema_config is not None
    assert {"candidates", "candidate_sources"} <= set(transform._output_schema_config.guaranteed_fields or ())

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"search_text": "Agency"}), mock_ctx)

    assert result.status == "success"
    assert result.row is not None
    emitted = result.row.to_dict()
    assert emitted["candidates"] == [{"links": ["/one", "/two"]}]
    assert emitted["candidate_sources"] == [
        {
            "links": {
                "source_url": emitted["fetch_url_final"],
                "record_selector": "main",
                "selector": "a",
                "attribute": "href",
                "match_policy": "all",
                "selected_count": 2,
            }
        }
    ]
    assert result.success_reason is not None
    assert "candidate_sources" in result.success_reason["fields_added"]


def test_structured_provenance_fingerprints_sensitive_final_url(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/directory"
    options["records"] = {
        "field": "candidates",
        "provenance_field": "candidate_sources",
        "selector": "main",
        "columns": [{"field": "name", "selector": "a", "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    response = httpx.Response(
        200,
        text="<main><a>Agency</a></main>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", f"https://{_TEST_IP}:443/final"),
    )
    final_url = "https://example.com/final?access_token=secret-value"
    call = mock_ctx.landscape.record_call.return_value

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), patch.object(transform, "_fetch_url", return_value=(response, final_url, call)):
        result = transform.process(make_pipeline_row({"search_text": "Agency"}), mock_ctx)

    assert result.status == "success"
    assert result.row is not None
    emitted = result.row.to_dict()
    assert "secret-value" not in repr(emitted)
    assert emitted["candidate_sources"][0]["name"]["source_url"] == emitted["fetch_url_final"]


def test_structured_provenance_field_rejects_output_collision() -> None:
    options = _make_basic_transform_options()
    options["records"] = {
        "field": "candidates",
        "provenance_field": "fetch_status",
        "selector": "main",
        "columns": [{"field": "name"}],
    }
    with pytest.raises(PluginConfigError, match="provenance_field"):
        WebScrapeTransform(options)


def test_resolved_record_links_require_exact_origin_configuration() -> None:
    options = _make_basic_transform_options()
    options["records"] = {
        "field": "candidates",
        "selector": "main a",
        "columns": [{"field": "detail_url", "attribute": "href", "resolve_url": True}],
    }
    with pytest.raises(PluginConfigError, match="allowed_origins"):
        WebScrapeTransform(options)


def test_resolved_record_link_uses_redirect_final_url_and_keeps_provenance_safe(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/start"
    options["http"]["allowed_origins"] = ["https://example.com"]
    options["records"] = {
        "field": "candidates",
        "provenance_field": "candidate_sources",
        "selector": "main a",
        "columns": [{"field": "detail_url", "attribute": "href", "resolve_url": True, "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    response = httpx.Response(
        200,
        text='<main><a href="../detail/42">Company</a></main>',
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", f"https://{_TEST_IP}:443/search/results/page"),
    )
    final_url = "https://example.com/search/results/page?access_token=secret-value"
    call = mock_ctx.landscape.record_call.return_value
    with patch("socket.getaddrinfo", _mock_getaddrinfo()), patch.object(transform, "_fetch_url", return_value=(response, final_url, call)):
        result = transform.process(make_pipeline_row({"search_text": "Company"}), mock_ctx)
    assert result.status == "success"
    assert result.row is not None
    emitted = result.row.to_dict()
    assert emitted["candidates"] == [{"detail_url": "https://example.com/search/detail/42"}]
    assert "secret-value" not in repr(emitted)
    assert emitted["candidate_sources"][0]["detail_url"]["source_url"] == emitted["fetch_url_final"]


def test_resolved_record_link_refusal_is_value_free(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/start"
    options["http"]["allowed_origins"] = ["https://example.com"]
    options["records"] = {
        "field": "candidates",
        "selector": "main a",
        "columns": [{"field": "detail_url", "attribute": "href", "resolve_url": True, "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    response = httpx.Response(
        200,
        text='<main><a href="/detail/42?access_token=secret-value">Company</a></main>',
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", f"https://{_TEST_IP}:443/search/results"),
    )
    call = mock_ctx.landscape.record_call.return_value
    with (
        patch("socket.getaddrinfo", _mock_getaddrinfo()),
        patch.object(transform, "_fetch_url", return_value=(response, "https://example.com/search/results", call)),
    ):
        result = transform.process(make_pipeline_row({"search_text": "Company"}), mock_ctx)
    assert result.status == "error"
    assert "secret-value" not in repr(result.reason)


@respx.mock
def test_search_candidate_limit_refuses_unreported_matches(mock_ctx: PluginContext) -> None:
    respx.get(f"https://{_TEST_IP}:443/directory").mock(
        return_value=httpx.Response(200, text="<main><li>One</li><li>Two</li></main>", headers={"content-type": "text/html"})
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/directory"
    options["records"] = {
        "field": "candidates",
        "selector": "main li",
        "max_records": 1,
        "columns": [{"field": "name", "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"search_text": "Shared Name"}), mock_ctx)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "content_extraction_failed"


@respx.mock
def test_search_required_column_refuses_incomplete_candidate(mock_ctx: PluginContext) -> None:
    respx.get(f"https://{_TEST_IP}:443/directory").mock(
        return_value=httpx.Response(200, text="<main><li>No link</li></main>", headers={"content-type": "text/html"})
    )
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/directory"
    options["records"] = {
        "field": "candidates",
        "selector": "main li",
        "columns": [{"field": "detail_path", "selector": "a", "attribute": "href", "required": True}],
    }
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"search_text": "Shared Name"}), mock_ctx)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "content_extraction_failed"


def test_search_record_selector_is_validated_at_config_time() -> None:
    options = _make_basic_transform_options()
    options["records"] = {
        "field": "candidates",
        "selector": "[",
        "columns": [{"field": "name", "selector": "a"}],
    }
    with pytest.raises(PluginConfigError, match="selector"):
        WebScrapeTransform(options)


def test_query_field_invalid_value_is_row_error_before_dns(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/search"
    options["query_fields"] = {"SearchText": "company_query"}
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS should not be reached")):
        result = transform.process(make_pipeline_row({"company_query": ["not", "a", "string"]}), mock_ctx)

    assert result.status == "error"


def test_query_options_reject_sensitive_names_and_duplicate_bindings() -> None:
    options = _make_basic_transform_options()
    options["query_fields"] = {"token": "company_query"}
    with pytest.raises(PluginConfigError, match="query"):
        WebScrapeTransform(options)

    options["query_fields"] = {"SearchText": "company_query"}
    options["query"] = {"SearchText": "static"}
    with pytest.raises(PluginConfigError, match="SearchText"):
        WebScrapeTransform(options)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "content-length",
        "Transfer-Encoding",
        "Connection",
        "Cookie",
        "Authorization",
        "Proxy-Authorization",
        "TE",
        "Upgrade",
        "Content-Type",
        "X-Abuse-Contact",
        "X-Custom",
    ],
)
def test_request_header_policy_rejects_unsafe_names_at_config_time(name: str) -> None:
    options = _make_basic_transform_options()
    options["headers"] = {name: "value"}
    with pytest.raises(PluginConfigError, match="header"):
        WebScrapeTransform(options)


def test_request_header_policy_rejects_case_insensitive_duplicates() -> None:
    options = _make_basic_transform_options()
    options["headers"] = {"Accept": "text/html"}
    options["header_fields"] = {"aCcEpT": "accept_value"}
    with pytest.raises(PluginConfigError, match="header"):
        WebScrapeTransform(options)


@pytest.mark.parametrize("value", ["", "value\r\nX-Injected: yes", "café", "a" * 1025, ["text/html"]])
def test_request_header_field_invalid_value_is_row_error_before_dns(mock_ctx: PluginContext, value: object) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/search"
    options["header_fields"] = {"Accept": "accept_value"}
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS should not be reached")):
        result = transform.process(make_pipeline_row({"accept_value": value}), mock_ctx)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "validation_failed"
    assert "value" not in str(result.reason)


@respx.mock
def test_request_headers_static_and_row_fields_reach_get_without_credentials(mock_ctx: PluginContext) -> None:
    route = respx.get(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="<main>Found</main>"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.com/search"
    options["headers"] = {"Accept": "text/html"}
    options["header_fields"] = {"Accept-Language": "language"}
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"language": "en-AU"}), mock_ctx)

    assert result.status == "success"
    assert transform.declared_input_fields == frozenset({"language"})
    assert route.calls[0].request.headers["accept"] == "text/html"
    assert route.calls[0].request.headers["accept-language"] == "en-AU"


@respx.mock
def test_request_header_audit_binds_wire_value_without_recording_plaintext(mock_ctx: PluginContext) -> None:
    respx.get(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="<main>Found</main>"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update(url="https://example.com/search", header_fields={"Accept-Language": "language"})
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    archived_requests: list[dict[str, object]] = []
    original_record_call = mock_ctx.landscape.record_call

    def capture_record_call(**kwargs: object) -> Call:
        request_data = kwargs["request_data"]
        assert isinstance(request_data, HTTPCallRequest)
        archived_requests.append(request_data.to_dict())
        return original_record_call(**kwargs)

    mock_ctx.landscape.record_call = capture_record_call
    env = dict(os.environ)
    env["ELSPETH_FINGERPRINT_KEY"] = "test-key-for-fingerprinting"
    env.pop("ELSPETH_ALLOW_RAW_SECRETS", None)
    with patch.dict(os.environ, env, clear=True), patch("socket.getaddrinfo", _mock_getaddrinfo()):
        first = transform.process(make_pipeline_row({"language": "en-AU"}), mock_ctx)
        second = transform.process(make_pipeline_row({"language": "en-NZ"}), mock_ctx)

    assert first.status == second.status == "success"
    assert len(archived_requests) == 2
    assert archived_requests[0]["headers"]["Accept-Language"].startswith("<fingerprint:")
    assert archived_requests[0]["headers"]["Accept-Language"] != archived_requests[1]["headers"]["Accept-Language"]
    assert "en-AU" not in str(archived_requests)
    assert "en-NZ" not in str(archived_requests)


@respx.mock
def test_request_headers_reach_post_form(mock_ctx: PluginContext) -> None:
    route = respx.post(f"https://{_TEST_IP}:443/search").mock(return_value=httpx.Response(200, text="<main>Found</main>"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update(url="https://example.com/search", method="POST", request_form_field="form", headers={"Accept": "text/html"})
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"form": [{"name": "q", "value": "Acme"}]}), mock_ctx)

    assert result.status == "success"
    assert route.calls[0].request.headers["accept"] == "text/html"
    assert route.calls[0].request.headers["content-type"] == "application/x-www-form-urlencoded; charset=utf-8"


@pytest.mark.parametrize(
    "redirect_url",
    ["https://other.example/final", "http://source.example/final", "https://source.example:8443/final"],
)
@respx.mock
def test_row_header_rejects_cross_origin_redirect_before_dns_or_dispatch(mock_ctx: PluginContext, redirect_url: str) -> None:
    first = respx.get(f"https://{_TEST_IP}:443/start").mock(return_value=httpx.Response(302, headers={"Location": redirect_url}))
    other = respx.get(f"https://{_TEST_IP}:443/final").mock(return_value=httpx.Response(200, text="other origin"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update(url="https://source.example/start", header_fields={"X-Requested-With": "request_kind"})
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    calls: list[tuple[CallType, HTTPCallRequest]] = []
    original_record_call = mock_ctx.landscape.record_call

    def record_call(**kwargs: object) -> Call:
        call_type = kwargs["call_type"]
        request_data = kwargs["request_data"]
        assert isinstance(call_type, CallType)
        assert isinstance(request_data, HTTPCallRequest)
        calls.append((call_type, request_data))
        return original_record_call(**kwargs)

    mock_ctx.landscape.record_call = record_call
    resolved_hosts: list[str] = []

    def resolve_initial_only(host: str, *_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        resolved_hosts.append(host)
        assert host == "source.example"
        return _mock_getaddrinfo()(host, 443)

    env = dict(os.environ)
    env["ELSPETH_FINGERPRINT_KEY"] = "test-key-for-fingerprinting"
    env.pop("ELSPETH_ALLOW_RAW_SECRETS", None)
    with patch.dict(os.environ, env, clear=True), patch("socket.getaddrinfo", resolve_initial_only):
        result = transform.process(make_pipeline_row({"request_kind": "canary-sensitive-value"}), mock_ctx)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["error_type"] == "SSRFBlockedError"
    assert first.call_count == 1
    assert other.call_count == 0
    assert resolved_hosts == ["source.example"]
    redirect_requests = [request for call_type, request in calls if call_type is CallType.HTTP_REDIRECT]
    assert len(redirect_requests) == 1
    assert "X-Requested-With" not in redirect_requests[0].headers
    assert "canary-sensitive-value" not in str(redirect_requests[0].to_dict())


@respx.mock
def test_row_header_remains_on_same_origin_redirect(mock_ctx: PluginContext) -> None:
    first = respx.get(f"https://{_TEST_IP}:443/start").mock(
        return_value=httpx.Response(302, headers={"Location": "https://source.example/final"})
    )
    second = respx.get(f"https://{_TEST_IP}:443/final").mock(return_value=httpx.Response(200, text="same origin"))
    options = _make_basic_transform_options()
    options.pop("url_field")
    options.update(url="https://source.example/start", header_fields={"X-Requested-With": "request_kind"})
    transform = WebScrapeTransform(options)
    transform.on_start(mock_ctx)
    env = dict(os.environ)
    env["ELSPETH_FINGERPRINT_KEY"] = "test-key-for-fingerprinting"
    env.pop("ELSPETH_ALLOW_RAW_SECRETS", None)
    with patch.dict(os.environ, env, clear=True), patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"request_kind": "public-search"}), mock_ctx)

    assert result.status == "success"
    assert first.call_count == second.call_count == 1
    assert first.calls[0].request.headers["x-requested-with"] == "public-search"
    assert second.calls[0].request.headers["x-requested-with"] == "public-search"


def test_fixed_url_invariant_probe_is_offline_and_restores_configured_url(mock_ctx: PluginContext) -> None:
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = "https://example.invalid/search"
    transform = WebScrapeTransform(options)
    rows = transform.forward_invariant_probe_rows(make_pipeline_row({"company_query": "Acme"}))

    def _probe_ip_only(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        assert host == "93.184.216.34", "probe must not resolve the configured hostname"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0))]

    with patch("socket.getaddrinfo", _probe_ip_only):
        result = transform.execute_forward_invariant_probe(rows, mock_ctx)

    assert result.status == "success"
    assert transform._url == "https://example.invalid/search"


@pytest.mark.parametrize(
    ("url", "include_url_field"),
    [(None, False), ("https://example.com/search", True)],
)
def test_web_scrape_requires_exactly_one_url_source(url: str | None, include_url_field: bool) -> None:
    options = _make_basic_transform_options()
    if not include_url_field:
        options.pop("url_field")
    if url is not None:
        options["url"] = url

    with pytest.raises(PluginConfigError, match=r"url.*url_field"):
        WebScrapeTransform(options)


@pytest.mark.parametrize("url", ["ftp://example.com/file", "https://169.254.169.254/latest", "https://example.com:0/"])
def test_web_scrape_rejects_unsafe_fixed_url_before_dns(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def _dns_forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("configuration must not resolve DNS")

    monkeypatch.setattr("socket.getaddrinfo", _dns_forbidden)
    options = _make_basic_transform_options()
    options.pop("url_field")
    options["url"] = url

    with pytest.raises(PluginConfigError, match="url"):
        WebScrapeTransform(options)


class TestUrlFieldMustNotNameACreatedField:
    """``url_field`` must name an ARRIVING column, never one web_scrape writes.

    ``url_field`` is read to get the URL and the created field is then written
    over it, so pointing both at one column makes the transform consume its own
    output. Nothing downstream catches it: the executor's collision check
    compares ``declared_output_fields`` against the INPUT KEYS OF THE ROW, so it
    fires only once a row actually carries the column, and under
    ``mode: observed`` there is no declared field for DAG validation to carry
    (elspeth-09dc6407f1).
    """

    def test_url_field_naming_the_content_target_is_rejected(self) -> None:
        with pytest.raises(PluginConfigError, match="url_field names 'page_text', which web_scrape itself creates"):
            WebScrapeTransform(_base_config(url_field="page_text", content_field="page_text"))

    def test_url_field_naming_the_fingerprint_target_is_rejected(self) -> None:
        with pytest.raises(PluginConfigError, match="url_field names 'page_fingerprint', which web_scrape itself creates"):
            WebScrapeTransform(_base_config(url_field="page_fingerprint"))

    def test_url_field_naming_a_hardcoded_operational_field_is_rejected(self) -> None:
        """The created set is not only the configurable targets.

        ``fetch_status`` and friends are literal constants in
        ``declared_output_fields``, and an upstream column really can be called
        that — a likelier authoring mistake than aiming ``url_field`` at the
        content target.
        """
        with pytest.raises(PluginConfigError, match="url_field names 'fetch_status', which web_scrape itself creates"):
            WebScrapeTransform(_base_config(url_field="fetch_status"))

    def test_the_error_names_the_offending_value_and_the_plugin(self) -> None:
        with pytest.raises(PluginConfigError) as excinfo:
            WebScrapeTransform(_base_config(url_field="fetch_url_final"))

        message = str(excinfo.value)
        assert "url_field names 'fetch_url_final', which web_scrape itself creates" in message
        assert "Point url_field at a column that ARRIVES on the row" in message

    def test_a_url_field_naming_an_arriving_column_still_constructs(self) -> None:
        """The arm that must not regress: rejecting legal configs is worse."""
        transform = WebScrapeTransform(_base_config())

        assert transform.declared_input_fields == frozenset({"url"})


# ===========================================================================
# allowed_hosts config validation
# ===========================================================================


class TestAllowedHostsConfig:
    """Config validation for allowed_hosts field."""

    def test_default_is_public_only(self) -> None:
        """Default allowed_hosts is public_only (no allowlist)."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                },
            }
        )
        assert t._allowed_ranges == ()

    def test_public_only_keyword(self) -> None:
        """public_only keyword produces empty allowed_ranges."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": "public_only",
                },
            }
        )
        assert t._allowed_ranges == ()

    def test_allow_private_keyword(self) -> None:
        """allow_private keyword produces 0.0.0.0/0 + ::/0."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": "allow_private",
                },
            }
        )
        range_strs = {str(r) for r in t._allowed_ranges}
        assert "0.0.0.0/0" in range_strs
        assert "::/0" in range_strs

    def test_cidr_list(self) -> None:
        """List of CIDR ranges parsed correctly."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": ["127.0.0.0/8", "10.0.0.0/8"],
                },
            }
        )
        range_strs = {str(r) for r in t._allowed_ranges}
        assert "127.0.0.0/8" in range_strs
        assert "10.0.0.0/8" in range_strs

    def test_single_ip_expanded_to_32(self) -> None:
        """Single IP address expanded to /32."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": ["127.0.0.1"],
                },
            }
        )
        range_strs = {str(r) for r in t._allowed_ranges}
        assert "127.0.0.1/32" in range_strs

    def test_ipv6_cidr_accepted(self) -> None:
        """IPv6 CIDR entries are accepted."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": ["::1/128"],
                },
            }
        )
        assert len(t._allowed_ranges) == 1

    def test_empty_list_rejected(self) -> None:
        """Empty list is rejected (ambiguous — use allow_private if intended)."""
        with pytest.raises((PluginConfigError, ValueError)):
            WebScrapeTransform(
                {
                    "schema": {"mode": "observed"},
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "Test",
                        "allowed_hosts": [],
                    },
                }
            )

    def test_invalid_cidr_rejected(self) -> None:
        """Unparseable CIDR entry crashes at config time."""
        with pytest.raises((PluginConfigError, ValueError)):
            WebScrapeTransform(
                {
                    "schema": {"mode": "observed"},
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "Test",
                        "allowed_hosts": ["not-a-cidr"],
                    },
                }
            )

    def test_invalid_keyword_rejected(self) -> None:
        """Unknown string keyword is rejected."""
        with pytest.raises((PluginConfigError, ValueError)):
            WebScrapeTransform(
                {
                    "schema": {"mode": "observed"},
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "Test",
                        "allowed_hosts": "allow_all",
                    },
                }
            )

    def test_keyword_case_sensitive(self) -> None:
        """Keywords are case-sensitive — 'Public_Only' is rejected."""
        with pytest.raises((PluginConfigError, ValueError)):
            WebScrapeTransform(
                {
                    "schema": {"mode": "observed"},
                    "url_field": "url",
                    "content_field": "content",
                    "fingerprint_field": "fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "Test",
                        "allowed_hosts": "Public_Only",
                    },
                }
            )

    def test_host_bits_set_normalized_by_strict_false(self) -> None:
        """CIDR with host bits set is silently normalized via strict=False."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": ["127.0.0.1/8"],
                },
            }
        )
        range_strs = {str(r) for r in t._allowed_ranges}
        assert "127.0.0.0/8" in range_strs

    def test_always_blocked_overlap_accepted_in_config(self) -> None:
        """Entries overlapping ALWAYS_BLOCKED_RANGES are accepted in config."""
        t = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Test",
                    "allowed_hosts": ["169.254.0.0/16"],
                },
            }
        )
        assert len(t._allowed_ranges) == 1


# ===========================================================================
# _parse_allowed_ranges unit tests
# ===========================================================================


class TestParseAllowedRanges:
    """Direct unit tests for _parse_allowed_ranges helper."""

    def test_mixed_ipv4_and_ipv6(self) -> None:
        """Mixed address families in a single list."""
        from elspeth.plugins.transforms.web_scrape import _parse_allowed_ranges

        result = _parse_allowed_ranges(["10.0.0.0/8", "::1/128"])
        assert len(result) == 2
        range_strs = {str(r) for r in result}
        assert "10.0.0.0/8" in range_strs
        assert "::1/128" in range_strs

    def test_single_ipv6_expanded_to_128(self) -> None:
        """Single IPv6 address without prefix is expanded to /128."""
        from elspeth.plugins.transforms.web_scrape import _parse_allowed_ranges

        result = _parse_allowed_ranges(["::1"])
        assert str(result[0]) == "::1/128"

    def test_host_bits_normalized(self) -> None:
        """Host bits are cleared by strict=False."""
        from elspeth.plugins.transforms.web_scrape import _parse_allowed_ranges

        result = _parse_allowed_ranges(["10.0.0.1/8"])
        assert str(result[0]) == "10.0.0.0/8"

    def test_returns_tuple(self) -> None:
        """Return type is tuple (immutable)."""
        from elspeth.plugins.transforms.web_scrape import _parse_allowed_ranges

        result = _parse_allowed_ranges(["127.0.0.0/8"])
        assert isinstance(result, tuple)


class TestOutputSchemaConfig:
    def test_fixed_input_output_schema_exposes_enriched_fields_for_type_validation(self):
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "fixed", "fields": ["url: str"]},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_hash",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Unit testing output schema config",
                },
            }
        )
        consumer_schema = create_schema_from_config(
            SchemaConfig.from_dict(
                {
                    "mode": "flexible",
                    "fields": [
                        "page_content: str",
                        "page_hash: str",
                        "fetch_status: int",
                        "fetch_url_final: str",
                        "fetch_url_final_ip: str",
                    ],
                }
            ),
            "WebScrapeDownstreamConsumer",
            allow_coercion=False,
        )

        result = check_compatibility(transform.output_schema, consumer_schema)

        assert result.compatible, result.error_message
        output_fields = transform.output_schema.model_fields
        assert output_fields["page_content"].annotation is str
        assert output_fields["page_hash"].annotation is str
        assert output_fields["fetch_status"].annotation is int
        assert output_fields["fetch_url_final"].annotation is str
        assert output_fields["fetch_url_final_ip"].annotation is str

        assert transform._output_schema_config is not None
        config_field_types = {field.name: field.field_type for field in transform._output_schema_config.fields or ()}
        assert config_field_types["page_content"] == "str"
        assert config_field_types["page_hash"] == "str"
        assert config_field_types["fetch_status"] == "int"
        assert config_field_types["fetch_url_final"] == "str"
        assert config_field_types["fetch_url_final_ip"] == "str"

    def test_guaranteed_fields(self):
        transform = WebScrapeTransform(
            {
                "schema": {"mode": "observed"},
                "url_field": "url",
                "content_field": "page_content",
                "fingerprint_field": "page_hash",
                "http": {
                    "abuse_contact": "test@example.com",
                    "scraping_reason": "Unit testing output schema config",
                },
            }
        )
        expected = frozenset(
            {
                "page_content",
                "page_hash",
                "fetch_status",
                "fetch_url_final",
                "fetch_url_final_ip",
            }
        )
        assert transform._output_schema_config is not None
        assert frozenset(transform._output_schema_config.guaranteed_fields) == expected


class TestWebScrapeOutputSemantics:
    def _build(self, **option_overrides):
        # WebScrapeConfig is a TransformDataConfig subclass — schema is
        # REQUIRED at construction. Omitting it raises PluginConfigError
        # which the validator's tolerant probe path silently absorbs;
        # the test would then pass vacuously without exercising
        # output_semantics() at all.
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        defaults = {
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "markdown",
            "http": {
                "abuse_contact": "x@example.com",
                "scraping_reason": "t",
                "timeout": 5,
                "allowed_hosts": "public_only",
            },
        }
        defaults.update(option_overrides)
        return get_shared_plugin_manager().create_transform("web_scrape", defaults)

    def test_text_compact_separator_declares_plain_text_compact(self):
        from elspeth.contracts.plugin_semantics import ContentKind, TextFraming

        ws = self._build(format="text", text_separator=" ")
        decl = ws.output_semantics()
        facts = next(f for f in decl.fields if f.field_name == "content")
        assert facts.content_kind is ContentKind.PLAIN_TEXT
        assert facts.text_framing is TextFraming.COMPACT
        assert facts.fact_code == "web_scrape.content.compact_text"
        assert facts.configured_by == ("format", "text_separator")

    def test_text_newline_separator_declares_plain_text_newline_framed(self):
        from elspeth.contracts.plugin_semantics import ContentKind, TextFraming

        ws = self._build(format="text", text_separator="\n")
        facts = next(f for f in ws.output_semantics().fields if f.field_name == "content")
        assert facts.content_kind is ContentKind.PLAIN_TEXT
        assert facts.text_framing is TextFraming.NEWLINE_FRAMED

    def test_markdown_declares_markdown_line_compatible(self):
        from elspeth.contracts.plugin_semantics import ContentKind, TextFraming

        ws = self._build(format="markdown")
        facts = next(f for f in ws.output_semantics().fields if f.field_name == "content")
        assert facts.content_kind is ContentKind.MARKDOWN
        assert facts.text_framing is TextFraming.LINE_COMPATIBLE

    def test_raw_declares_html_raw_with_unconstrained_framing(self):
        """The raw value is the fetched page verbatim: a str whose framing is
        whatever the server sent, which no configuration settles. That is the
        UNCONSTRAINED claim by definition. NOT_TEXT — a positive claim that the
        value is not text at all — was false of a str of HTML, and it made
        ``raw -> document`` (archive this page to a file) a false authoring
        CONFLICT (elspeth-24c04df25f)."""
        from elspeth.contracts.plugin_semantics import ContentKind, TextFraming

        ws = self._build(format="raw")
        facts = next(f for f in ws.output_semantics().fields if f.field_name == "content")
        assert facts.content_kind is ContentKind.HTML_RAW
        assert facts.text_framing is TextFraming.UNCONSTRAINED

    def test_raw_framing_grades_the_framing_constrained_consumers_correctly(self):
        """The three consumers that constrain framing, graded against raw's claim.

        ``raw -> text`` must stay refused — a fetched page carries newlines and
        TextSink diverts on CR/LF, so {COMPACT} correctly excludes it. But
        ``raw -> line_explode`` is legitimate: splitting arbitrary fetched text
        into line rows is the same operation as splitting generated text, which
        line_explode exists to bless. (``raw -> document`` is pinned from the
        sink's side in test_document_sink.py.)
        """
        from elspeth.contracts.plugin_semantics import SemanticOutcome, compare_semantic
        from elspeth.plugins.transforms.line_explode import _build_line_explode_input_requirements
        from elspeth.plugins.transforms.web_scrape import _build_web_scrape_output_semantics

        facts = _build_web_scrape_output_semantics(content_field="content", format="raw", text_separator="\n").fields[0]

        from elspeth.plugins.sinks.text_sink import TextSink

        text_requirement = (
            TextSink({"path": "/tmp/lines.txt", "field": "content", "schema": {"mode": "observed"}}).input_semantic_requirements().fields[0]
        )
        line_explode_requirement = _build_line_explode_input_requirements(source_field="content").fields[0]

        assert compare_semantic(facts, text_requirement) is SemanticOutcome.CONFLICT
        assert compare_semantic(facts, line_explode_requirement) is SemanticOutcome.SATISFIED

    def test_custom_content_field_changes_semantic_field_name(self):
        ws = self._build(format="text", text_separator="\n", content_field="body")
        facts = next(f for f in ws.output_semantics().fields if f.field_name == "body")
        assert facts.field_name == "body"


class TestWebScrapeValueTypeBindsToTheFieldNotTheTopology:
    """web_scrape declaring STR must not cost json_explode its use case.

    Every format CONFLICTS into ``json_explode`` when array_field IS the
    scraped content field, and that refusal is correct: json_explode raises
    TypeError on a str rather than parsing it, so the composition dies on row 1
    (pinned end-to-end by TestWebScrapeValueTypeDeclarationSideEffect).

    The regression to guard is the OTHER half — that the refusal binds to a
    WIRING and not to the pair of plugins. ``_find_producer_facts`` matches
    facts to requirements by exact field name, so a list-bearing field carried
    alongside the scraped content compares UNKNOWN and stays authorable under
    json_explode's WARN. Losing that would re-enter elspeth-7a2c9a24c3 from the
    producer side, which is the claim this class exists to falsify.
    """

    def _content_facts(self, *, fmt: str, separator: str):
        from elspeth.plugins.transforms.web_scrape import _build_web_scrape_output_semantics

        return _build_web_scrape_output_semantics(
            content_field="content",
            format=fmt,
            text_separator=separator,
        ).fields[0]

    @pytest.mark.parametrize(
        ("fmt", "separator"),
        [("markdown", "\n"), ("text", "\n"), ("text", " "), ("raw", "\n")],
    )
    def test_every_format_declares_str_and_conflicts_on_the_content_field(self, fmt: str, separator: str) -> None:
        from elspeth.contracts.plugin_semantics import (
            SemanticOutcome,
            SemanticValueType,
            compare_semantic,
        )
        from elspeth.plugins.transforms.json_explode import _build_json_explode_input_requirements

        facts = self._content_facts(fmt=fmt, separator=separator)
        requirement = _build_json_explode_input_requirements(array_field="content").fields[0]

        assert facts.value_type is SemanticValueType.STR
        assert compare_semantic(facts, requirement) is SemanticOutcome.CONFLICT

    @pytest.mark.parametrize(
        ("fmt", "separator"),
        [("markdown", "\n"), ("text", "\n"), ("text", " "), ("raw", "\n")],
    )
    def test_a_sibling_field_stays_authorable_for_every_format(self, fmt: str, separator: str) -> None:
        """web_scrape declares facts for content_field ONLY, so exploding a
        different column is untouched: UNKNOWN, graded advisory by WARN."""
        from elspeth.contracts.plugin_semantics import (
            SemanticOutcome,
            UnknownSemanticPolicy,
            compare_semantic,
        )
        from elspeth.plugins.transforms.json_explode import _build_json_explode_input_requirements
        from elspeth.plugins.transforms.web_scrape import _build_web_scrape_output_semantics
        from elspeth.web.composer._semantic_validator import _find_producer_facts

        declaration = _build_web_scrape_output_semantics(
            content_field="content",
            format=fmt,
            text_separator=separator,
        )
        requirement = _build_json_explode_input_requirements(array_field="items").fields[0]

        facts = _find_producer_facts(declaration, "items")
        assert facts is None, "web_scrape must not claim a field it does not write"
        assert compare_semantic(facts, requirement) is SemanticOutcome.UNKNOWN
        assert requirement.unknown_policy is UnknownSemanticPolicy.WARN, (
            "UNKNOWN is only authorable because json_explode grades it as an advisory"
        )


class TestWebScrapeAssistance:
    def test_returns_assistance_for_compact_text_issue(self):
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        result = WebScrapeTransform.get_agent_assistance(
            issue_code="web_scrape.content.compact_text",
        )
        assert result is not None
        assert result.plugin_name == "web_scrape"
        assert result.issue_code == "web_scrape.content.compact_text"
        # Suggested fixes mention configuration knobs only — no values.
        assert any("text_separator" in fix for fix in result.suggested_fixes)
        assert any("markdown" in fix.lower() for fix in result.suggested_fixes)

    def test_returns_none_for_unknown_issue(self):
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        assert WebScrapeTransform.get_agent_assistance(issue_code="nope.unknown") is None

    def test_returns_discovery_assistance_when_no_issue_code(self):
        """Phase 1 dual-use: issue_code=None returns discovery-time hints (Jam-1)."""
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        result = WebScrapeTransform.get_agent_assistance(issue_code=None)
        assert result is not None
        assert result.plugin_name == "web_scrape"
        assert result.issue_code is None
        assert result.composer_hints  # non-empty
        # web_scrape's discovery-time hints document the SSRF guard and the
        # audit recording — those are the load-bearing properties.
        joined = "\n".join(result.composer_hints)
        assert "audit" in joined.lower()

    def test_assistance_does_not_leak_secret_options(self):
        """Sentinel test: configured option values must not bleed into assistance prose.

        Construct a plugin with sentinel-laced options (abuse_contact, scraping_reason
        — the realistic credential-shaped leak surface) and assert the returned
        PluginAssistance carries none of those raw values. Also covers
        output_semantics() against accidental value-bearing fields.

        Field names must be Python identifiers (config layer enforces this), so
        the sentinel only appears in HTTP option values where leakage would
        actually be a B5 incident.
        """
        from elspeth.contracts.plugin_assistance import PluginAssistance, PluginAssistanceExample
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        sentinel_contact = "SENTINEL_LEAK_CONTACT@example.com"
        sentinel_reason = "SENTINEL_LEAK_REASON_credential_shape"

        plugin = get_shared_plugin_manager().create_transform(
            "web_scrape",
            {
                "schema": {"mode": "flexible", "fields": ["url: str"]},
                "required_input_fields": ["url"],
                "url_field": "url",
                "content_field": "content",
                "fingerprint_field": "fingerprint",
                "format": "text",
                "text_separator": " ",
                "http": {
                    "abuse_contact": sentinel_contact,
                    "scraping_reason": sentinel_reason,
                    "timeout": 5,
                    "allowed_hosts": "public_only",
                },
            },
        )

        # output_semantics() must not echo any HTTP option value.
        decl = plugin.output_semantics()
        for fact in decl.fields:
            assert sentinel_contact not in fact.field_name
            assert sentinel_contact not in fact.fact_code
            assert all(sentinel_contact not in entry for entry in fact.configured_by)
            assert sentinel_reason not in fact.field_name
            assert sentinel_reason not in fact.fact_code
            assert all(sentinel_reason not in entry for entry in fact.configured_by)

        result = WebScrapeTransform.get_agent_assistance(
            issue_code="web_scrape.content.compact_text",
        )
        assert result is not None
        assert isinstance(result, PluginAssistance)

        def _scan(text: str) -> None:
            assert sentinel_contact not in text
            assert sentinel_reason not in text

        _scan(result.plugin_name)
        if result.issue_code is not None:
            _scan(result.issue_code)
        _scan(result.summary)
        for fix in result.suggested_fixes:
            _scan(fix)
        for hint in result.composer_hints:
            _scan(hint)
        for example in result.examples:
            assert isinstance(example, PluginAssistanceExample)
            _scan(example.title)
            for mapping_field in (example.before, example.after):
                if mapping_field is None:
                    continue
                for key, value in mapping_field.items():
                    _scan(str(key))
                    _scan(str(value))


class TestWebScrapeSecretLeakage:
    SENTINEL = "PASSWORD_SENTINEL_x9q7r3"

    def test_sentinel_url_not_in_output_semantics_or_assistance(self):
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        ws = get_shared_plugin_manager().create_transform(
            "web_scrape",
            {
                "schema": {"mode": "flexible", "fields": ["url: str"]},
                "required_input_fields": ["url"],
                "url_field": "url",
                "content_field": f"content_{self.SENTINEL}",  # field name SHOULD appear
                "fingerprint_field": "fingerprint",
                "format": "text",
                "text_separator": " ",
                "http": {
                    "abuse_contact": f"x+{self.SENTINEL}@example.com",
                    "scraping_reason": f"reason-{self.SENTINEL}",
                    "timeout": 5,
                    "allowed_hosts": "public_only",
                },
            },
        )

        # Output semantics: the configured content_field name DOES include
        # the sentinel - that's not a leak, the user wrote that field name.
        # What MUST NOT appear: the abuse_contact email, the scraping_reason.
        decl = ws.output_semantics()
        decl_repr = repr(decl)
        assert f"x+{self.SENTINEL}" not in decl_repr
        assert f"reason-{self.SENTINEL}" not in decl_repr

        # Assistance is class-level, no instance state - but verify anyway.
        assistance = WebScrapeTransform.get_agent_assistance(
            issue_code="web_scrape.content.compact_text",
        )
        assistance_repr = repr(assistance)
        assert self.SENTINEL not in assistance_repr


class TestWebScrapeGuaranteedFieldsOptionKeyGuard:
    """Reject the LLM-composer footgun of listing option-key names in guaranteed_fields.

    A bad config has been observed where the literal strings 'content_field' and
    'fingerprint_field' (names of WebScrapeConfig options) were placed inside
    schema.guaranteed_fields instead of the column names those options point at.
    The SchemaConfigModeContract catches this at runtime, but we want
    plugin-validate-time rejection so composer authors (human or LLM) see an
    actionable error before the pipeline runs.
    """

    def test_option_key_names_in_guaranteed_fields_rejected(self) -> None:
        """Negative: literal option-key strings in guaranteed_fields are rejected."""
        bad_config = {
            "schema": {
                "mode": "observed",
                "guaranteed_fields": ["content_field", "fingerprint_field"],
            },
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing option-key guard",
            },
        }

        with pytest.raises(PluginConfigError) as exc_info:
            WebScrapeTransform(bad_config)

        message = str(exc_info.value)
        # Both bad entries are named.
        assert "'content_field'" in message
        assert "'fingerprint_field'" in message
        # The suggested substitutions (the actual configured column-name values)
        # are present so the LLM can self-correct.
        assert "'content'" in message
        assert "'content_fingerprint'" in message
        # The message explains that those strings are option-key names, not column names.
        assert "option-key" in message or "option key" in message

    def test_url_field_in_guaranteed_fields_also_rejected(self) -> None:
        """Negative: 'url_field' in guaranteed_fields is also caught (covers all three keys)."""
        bad_config = {
            "schema": {
                "mode": "observed",
                "guaranteed_fields": ["url_field"],
            },
            "url_field": "page_url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing option-key guard",
            },
        }

        with pytest.raises(PluginConfigError) as exc_info:
            WebScrapeTransform(bad_config)

        message = str(exc_info.value)
        assert "'url_field'" in message
        # Suggests substituting the configured value
        assert "'page_url'" in message

    def test_correct_column_names_in_guaranteed_fields_accepted(self) -> None:
        """Positive regression-guard: actual column names in guaranteed_fields construct cleanly."""
        good_config = {
            "schema": {
                "mode": "observed",
                "guaranteed_fields": ["url", "content", "content_fingerprint"],
            },
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing positive guard path",
            },
        }

        transform = WebScrapeTransform(good_config)

        assert transform._content_field == "content"
        assert transform._fingerprint_field == "content_fingerprint"

    def test_guaranteed_fields_absent_does_not_trigger_guard(self) -> None:
        """Edge case: a schema with no guaranteed_fields must not crash inside the new validator."""
        config_without_guaranteed = {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing absent-guaranteed_fields path",
            },
        }

        # Must not raise.
        transform = WebScrapeTransform(config_without_guaranteed)
        assert transform._content_field == "content"

    def test_option_key_names_in_required_fields_rejected(self) -> None:
        """Negative: literal option-key strings in schema.required_fields are rejected.

        ``required_fields`` is a sibling column-name list on SchemaConfig and is
        equally vulnerable to the same hallucination as ``guaranteed_fields``.
        """
        bad_config = {
            "schema": {
                "mode": "observed",
                "required_fields": ["content_field"],
            },
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing required_fields option-key guard",
            },
        }

        with pytest.raises(PluginConfigError) as exc_info:
            WebScrapeTransform(bad_config)

        message = str(exc_info.value)
        # The offending list is named so the author knows where to fix.
        assert "required_fields" in message
        # The bad entry and the suggested substitution are both surfaced.
        assert "'content_field'" in message
        assert "'content'" in message
        assert "option-key" in message or "option key" in message

    def test_option_key_names_in_audit_fields_rejected(self) -> None:
        """Negative: literal option-key strings in schema.audit_fields are rejected.

        ``audit_fields`` is the third sibling column-name list on SchemaConfig
        and must be guarded for the same reason.
        """
        bad_config = {
            "schema": {
                "mode": "observed",
                "audit_fields": ["fingerprint_field"],
            },
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing audit_fields option-key guard",
            },
        }

        with pytest.raises(PluginConfigError) as exc_info:
            WebScrapeTransform(bad_config)

        message = str(exc_info.value)
        # The offending list is named so the author knows where to fix.
        assert "audit_fields" in message
        # The bad entry and the suggested substitution are both surfaced.
        assert "'fingerprint_field'" in message
        assert "'content_fingerprint'" in message
        assert "option-key" in message or "option key" in message

    def test_column_literally_named_like_option_key_is_accepted(self) -> None:
        """Degenerate-same-name edge case: when the configured value equals the option key.

        If an operator configures ``content_field: "content_field"``, the column
        on the row really is called ``content_field``. Listing ``"content_field"``
        in ``guaranteed_fields`` is then *correct* — the schema entry names a
        column that genuinely exists on the row. The guard must skip these
        entries instead of false-positiving.
        """
        degenerate_config = {
            "schema": {
                "mode": "observed",
                "guaranteed_fields": ["url", "content_field", "content_fingerprint"],
            },
            "url_field": "url",
            "content_field": "content_field",  # column literally named 'content_field'
            "fingerprint_field": "content_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "Testing degenerate same-name path",
            },
        }

        # Must not raise: 'content_field' is a legitimate column name here
        # because the knob value matches the knob key.
        transform = WebScrapeTransform(degenerate_config)
        assert transform._content_field == "content_field"
        assert transform._fingerprint_field == "content_fingerprint"


# ---------------------------------------------------------------------------
# B3.9 -- unenumerated 4xx codes must not be fingerprinted as content
# ---------------------------------------------------------------------------


def _make_basic_transform() -> WebScrapeTransform:
    """Minimal WebScrapeTransform for error-handling tests."""
    return WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "B3.9 regression test",
            },
        }
    )


@respx.mock
def test_b3_9_http_400_returns_error_not_fingerprint(mock_ctx):
    """HTTP 400 must return an error result - not fingerprint the error-page body (B3.9)."""
    error_body = "<html><body><p>Bad Request</p></body></html>"
    respx.get(f"https://{_TEST_IP}:443/bad").mock(return_value=httpx.Response(400, text=error_body))

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/bad"}), mock_ctx)

    assert result.status == "error", f"Expected error for HTTP 400, got {result.status!r}"
    # Must not have fabricated a fingerprint of the error-page body
    assert "page_fingerprint" not in (result.row or {}), "HTTP 400 must not produce a fingerprint"
    assert "page_content" not in (result.row or {}), "HTTP 400 must not produce content"
    # Error reason must record the HTTP status
    assert "400" in result.reason.get("error", ""), f"Error reason should mention 400, got {result.reason}"


@respx.mock
def test_b3_9_http_410_returns_error_not_fingerprint(mock_ctx):
    """HTTP 410 Gone must return an error result - not fingerprint the error-page body (B3.9).

    410 is especially dangerous for long-running monitoring: the page is permanently
    gone, but the old code would fingerprint the error-page body as if it were content,
    corrupting change-detection.
    """
    error_body = "<html><body><p>This page is gone.</p></body></html>"
    respx.get(f"https://{_TEST_IP}:443/gone").mock(return_value=httpx.Response(410, text=error_body))

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/gone"}), mock_ctx)

    assert result.status == "error", f"Expected error for HTTP 410, got {result.status!r}"
    assert "page_fingerprint" not in (result.row or {}), "HTTP 410 must not produce a fingerprint"
    assert "page_content" not in (result.row or {}), "HTTP 410 must not produce content"
    assert "410" in result.reason.get("error", ""), f"Error reason should mention 410, got {result.reason}"


@respx.mock
def test_b3_9_http_408_returns_error_retryable(mock_ctx):
    """HTTP 408 Request Timeout must be retryable (mirrors 429 behaviour) (B3.9)."""
    respx.get(f"https://{_TEST_IP}:443/slow").mock(return_value=httpx.Response(408))

    from elspeth.plugins.transforms.web_scrape_errors import ClientError

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()), pytest.raises(ClientError) as exc_info:
        transform.process(make_pipeline_row({"url": "https://example.com/slow"}), mock_ctx)

    assert exc_info.value.retryable is True, "HTTP 408 must be retryable"
    assert "408" in str(exc_info.value)


# ---------------------------------------------------------------------------
# B3.10 -- no response size cap / no content-type guard
# ---------------------------------------------------------------------------


@respx.mock
def test_b3_10_oversized_response_returns_error_not_fingerprint(mock_ctx):
    """A response exceeding max_body_bytes must return error, not a fingerprinted body (B3.10)."""
    # 5 MB of 'A' -- well over any reasonable default
    big_body = "A" * (5 * 1024 * 1024)
    respx.get(f"https://{_TEST_IP}:443/huge").mock(return_value=httpx.Response(200, text=big_body, headers={"content-type": "text/html"}))

    # Configure a small limit (1 KB) so the 5 MB body triggers the cap
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "B3.10 size cap test",
                "max_body_bytes": 1024,
            },
        }
    )
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/huge"}), mock_ctx)

    assert result.status == "error", f"Expected error for oversized response, got {result.status!r}"
    assert "page_fingerprint" not in (result.row or {}), "Oversized response must not produce a fingerprint"
    assert "page_content" not in (result.row or {}), "Oversized response must not produce content"
    reason = result.reason
    assert "body_too_large" in reason.get("reason", "") or "body_too_large" in reason.get("error", ""), (
        f"Error reason should indicate body too large, got {reason}"
    )


def test_b3_10_web_scrape_wires_max_body_bytes_into_audited_http_client(mock_ctx):
    """web_scrape's max_body_bytes must become the shared client's streaming cap."""
    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "test@example.com",
                "scraping_reason": "B3.10 streaming cap wiring test",
                "max_body_bytes": 1234,
            },
        }
    )
    transform.on_start(mock_ctx)
    safe_request = SSRFSafeRequest(
        original_url="https://example.com/ok",
        resolved_ip=_TEST_IP,
        host_header="example.com",
        port=443,
        path="/ok",
        scheme="https",
        bare_hostname="example.com",
    )
    response = httpx.Response(
        200,
        text="<html>ok</html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", f"https://{_TEST_IP}:443/ok"),
    )

    with patch("elspeth.plugins.transforms.web_scrape.AuditedHTTPClient") as client_cls:
        client = _AuditedHTTPClientFake((response, "https://example.com/ok", mock_ctx.landscape.record_call.return_value))
        client_cls.return_value = client

        transform._fetch_url(safe_request, mock_ctx)

    assert client_cls.call_args.kwargs["max_response_body_bytes"] == 1234
    assert client.get_ssrf_safe_call_count == 1
    assert client.close_call_count == 1


@respx.mock
def test_b3_10_binary_content_type_returns_error(mock_ctx):
    """A binary content-type (application/octet-stream) must return error, not a fingerprint (B3.10)."""
    binary_body = bytes(range(256)) * 100  # 25.6 KB of raw bytes
    respx.get(f"https://{_TEST_IP}:443/binary").mock(
        return_value=httpx.Response(200, content=binary_body, headers={"content-type": "application/octet-stream"})
    )

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/binary"}), mock_ctx)

    assert result.status == "error", f"Expected error for binary content-type, got {result.status!r}"
    assert "page_fingerprint" not in (result.row or {}), "Binary response must not produce a fingerprint"
    assert "page_content" not in (result.row or {}), "Binary response must not produce content"
    reason = result.reason
    assert "non_text_content_type" in reason.get("reason", "") or "content_type" in reason.get("error", "").lower(), (
        f"Error reason should indicate non-text content type, got {reason}"
    )


@respx.mock
@pytest.mark.parametrize("headers, expected", [({}, None), ({"content-type": ""}, "")])
def test_missing_and_empty_content_type_remain_distinct_in_error(mock_ctx, headers, expected):
    """Rejected external headers retain absence rather than fabricating bytes."""
    respx.get(f"https://{_TEST_IP}:443/page").mock(
        return_value=httpx.Response(200, content=b"<html><body>Hello</body></html>", headers=headers)
    )
    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "error"
    assert result.reason["reason"] == "non_text_content_type"
    assert result.reason["content_type"] == expected
    assert result.row is None


@respx.mock
def test_b3_10_image_content_type_returns_error(mock_ctx):
    """An image content-type (image/png) must return error, not a fingerprint (B3.10)."""
    respx.get(f"https://{_TEST_IP}:443/img").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"})
    )

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/img"}), mock_ctx)

    assert result.status == "error", f"Expected error for image content-type, got {result.status!r}"
    assert "page_fingerprint" not in (result.row or {}), "Image response must not produce a fingerprint"


@respx.mock
def test_b3_10_text_html_passes_content_type_guard(mock_ctx):
    """text/html responses must pass the content-type guard and succeed normally (B3.10)."""
    html = "<html><body><p>Hello</p></body></html>"
    respx.get(f"https://{_TEST_IP}:443/page").mock(
        return_value=httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
    )

    transform = _make_basic_transform()
    transform.on_start(mock_ctx)

    with patch("socket.getaddrinfo", _mock_getaddrinfo()):
        result = transform.process(make_pipeline_row({"url": "https://example.com/page"}), mock_ctx)

    assert result.status == "success", f"text/html should succeed, got {result.status!r}: {result.reason}"
    assert result.row["page_fingerprint"] is not None
