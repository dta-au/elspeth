"""Plugin review P5 sweep: web_scrape ``fingerprint_mode: full`` and shared URL control characters.

Finding #5 (2026-11-11 plugin review): ``fingerprint_mode: full`` was named and
described as covering the full fetch response context, but it hashed only the
extracted content without whitespace normalisation. A change of HTTP status,
final URL, content type or raw body that left the extracted text unchanged was
invisible to it. ``full`` now hashes the extracted content together with, for
every fetched page in order, the HTTP status, the persisted final URL
(``fetch_url_final`` rendering), the raw ``Content-Type`` header and the
SHA-256 of the raw response body. ``content`` is unchanged.

Shared URL helpers: ``core/security/web.py`` admitted DEL (``\\x7f``) everywhere
and, with an empty ``allowed_origins``, every C0 control character, although
httpx refuses them all with ``httpx.InvalidURL``. Only web_scrape refused them,
in its own row path. Every SSRF / origin helper now refuses ASCII C0 and DEL
with ``SSRFBlockedError(kind="malformed_url")``, so every caller (blob_fetch,
the browser boundary, discovered links, the HTTP clients) inherits it.

Every test fails at an assertion on the pre-fix tree: refusals are captured
explicitly rather than allowed to escape as uncaught exceptions, and the new
fingerprint API is asserted to exist before it is used.
"""

from __future__ import annotations

import hashlib
import importlib.util
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import Call
from elspeth.contracts.call_mode import ReplaySSRFRequest
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.security import web as security_web
from elspeth.plugins.transforms import web_scrape_fingerprint
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.mock_audit import mock_item_audit_authority

_PUBLIC_IP = "93.184.216.34"

# ---------------------------------------------------------------------------
# #5: fingerprint_mode 'full' covers the fetch response context
# ---------------------------------------------------------------------------


def _fingerprint_api() -> tuple[Any, Any]:
    assert importlib.util.find_spec("elspeth.plugins.transforms.web_scrape_fingerprint") is not None
    namespace = vars(web_scrape_fingerprint)
    assert "FetchResponseContext" in namespace, "web_scrape_fingerprint must define FetchResponseContext"
    return namespace["compute_fingerprint"], namespace["FetchResponseContext"]


def _context(fetch_context_cls: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "status_code": 200,
        "final_url": "https://example.com/page",
        "content_type": "text/html",
        "body_sha256": hashlib.sha256(b"<p>hi</p>").hexdigest(),
    }
    values.update(overrides)
    return fetch_context_cls(**values)


def test_full_fingerprint_changes_with_each_response_context_field() -> None:
    compute, ctx_cls = _fingerprint_api()
    base = compute("hi", "full", responses=(_context(ctx_cls),))
    variants = {
        "status_code": _context(ctx_cls, status_code=203),
        "final_url": _context(ctx_cls, final_url="https://example.com/moved"),
        "content_type": _context(ctx_cls, content_type="text/html; charset=utf-8"),
        "content_type_absent": _context(ctx_cls, content_type=None),
        "body_sha256": _context(ctx_cls, body_sha256=hashlib.sha256(b"<p>hi</p><!-- x -->").hexdigest()),
    }
    for name, variant in variants.items():
        assert compute("hi", "full", responses=(variant,)) != base, f"full fingerprint ignores {name}"
    assert compute("hi ", "full", responses=(_context(ctx_cls),)) != base, "full fingerprint must hash content exactly"
    # Page order and page count are part of the covered context.
    other = _context(ctx_cls, final_url="https://example.com/page2")
    first = _context(ctx_cls)
    assert compute("hi", "full", responses=(first, other)) != compute("hi", "full", responses=(other, first))
    assert compute("hi", "full", responses=(first, other)) != base


def test_content_fingerprint_is_unchanged_and_ignores_response_context() -> None:
    compute, ctx_cls = _fingerprint_api()
    expected = hashlib.sha256(b"a b").hexdigest()
    assert compute(" a \n b ", "content") == expected
    assert compute(" a \n b ", "content", responses=(_context(ctx_cls, status_code=203),)) == expected


def test_full_fingerprint_requires_response_context() -> None:
    compute, _ctx_cls = _fingerprint_api()
    refused: ValueError | None = None
    try:
        compute("hi", "full")
    except ValueError as exc:
        refused = exc
    assert refused is not None, "full fingerprint without response context must be refused, not degrade to content-only"


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


def _plugin_context() -> PluginContext:
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
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]


def _scrape(mode: str, url: str, response: httpx.Response) -> dict[str, Any]:
    from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

    transform = WebScrapeTransform(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "fingerprint",
            "format": "text",
            "fingerprint_mode": mode,
            "http": {"abuse_contact": "test@example.com", "scraping_reason": "fingerprint mode unit test"},
        }
    )
    context = _plugin_context()
    with respx.mock:
        respx.route().mock(return_value=response)
        transform.on_start(context)
        with patch("socket.getaddrinfo", _dns):
            result = transform.process(make_pipeline_row({"url": url}), context)
    assert result.status == "success", result.reason
    assert result.row is not None
    return result.row.to_dict()


_PAGE = "https://example.com/page"


def _html(body: str = "<p>hi</p>", *, status: int = 200, content_type: str = "text/html") -> httpx.Response:
    return httpx.Response(status, headers={"content-type": content_type}, text=body)


_CONTEXT_CHANGES: list[Any] = [
    pytest.param(_PAGE, lambda: _html(status=203), id="status"),
    pytest.param("https://example.com/other", _html, id="final-url"),
    pytest.param(_PAGE, lambda: _html(content_type="text/html; charset=utf-8"), id="content-type"),
    pytest.param(_PAGE, lambda: _html("<p>hi</p><!-- revised -->"), id="raw-body"),
]


@pytest.mark.parametrize(("url", "changed"), _CONTEXT_CHANGES)
def test_web_scrape_full_fingerprint_detects_response_context_change(url: str, changed: Callable[[], httpx.Response]) -> None:
    baseline_full = _scrape("full", _PAGE, _html())
    changed_full = _scrape("full", url, changed())
    assert changed_full["content"] == baseline_full["content"], "fixture must leave the extracted content unchanged"
    assert changed_full["fingerprint"] != baseline_full["fingerprint"], "full fingerprint missed a response-context change"


@pytest.mark.parametrize(("url", "changed"), _CONTEXT_CHANGES)
def test_web_scrape_content_fingerprint_ignores_response_context_change(url: str, changed: Callable[[], httpx.Response]) -> None:
    baseline = _scrape("content", _PAGE, _html())
    other = _scrape("content", url, changed())
    assert other["fingerprint"] == baseline["fingerprint"] == hashlib.sha256(b"hi").hexdigest()


def test_web_scrape_full_fingerprint_is_exactly_the_documented_context() -> None:
    compute, ctx_cls = _fingerprint_api()
    body = "<p>hi</p>"
    row = _scrape("full", _PAGE, _html(body, content_type="text/html; charset=utf-8"))
    expected = compute(
        row["content"],
        "full",
        responses=(
            ctx_cls(
                status_code=row["fetch_status"],
                final_url=row["fetch_url_final"],
                content_type="text/html; charset=utf-8",
                body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            ),
        ),
    )
    assert row["fingerprint"] == expected


def test_fingerprint_mode_description_names_the_covered_context() -> None:
    from elspeth.plugins.transforms.web_scrape import WebScrapeConfig

    description = WebScrapeConfig.model_fields["fingerprint_mode"].description
    assert description is not None
    assert "neither mode covers" not in description
    for term in ("status", "final URL", "content type", "raw body"):
        assert term in description, f"fingerprint_mode description must name {term!r} as covered by full"


# ---------------------------------------------------------------------------
# Shared SSRF / origin helpers refuse ASCII C0 and DEL (httpx's predicate)
# ---------------------------------------------------------------------------

_CONTROL_URLS = [
    pytest.param("https://example.com/\x7f", id="del-in-path"),
    pytest.param("https://exa\x7fmple.com/", id="del-in-host"),
    pytest.param("https://example.com/page\n", id="trailing-newline"),
    pytest.param("https://example.com/a\tb", id="tab"),
    pytest.param("https://example.com/\x00", id="nul"),
    pytest.param("https://example.com/\x01", id="soh"),
    pytest.param("https://example.com/\x1f", id="unit-separator"),
    pytest.param("https://example.com/page\r", id="carriage-return"),
]

_ORIGINS = (("https", "example.com", 443),)


def _no_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    looked_up: list[str] = []

    def _resolve(hostname: str) -> list[str]:
        looked_up.append(hostname)
        return [_PUBLIC_IP]

    monkeypatch.setattr(security_web, "_resolve_hostname", _resolve)
    return looked_up


def _refusal(call: Callable[[], object]) -> security_web.SSRFBlockedError | None:
    try:
        call()
    except security_web.SSRFBlockedError as exc:
        return exc
    return None


def _assert_malformed(refused: security_web.SSRFBlockedError | None, helper: str) -> None:
    assert refused is not None, f"{helper} admitted a URL containing an ASCII control character"
    assert refused.kind == "malformed_url"


@pytest.mark.parametrize("url", _CONTROL_URLS)
def test_validate_url_for_ssrf_refuses_control_characters_before_dns(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up = _no_dns(monkeypatch)
    _assert_malformed(_refusal(lambda: security_web.validate_url_for_ssrf(url)), "validate_url_for_ssrf")
    assert looked_up == []


@pytest.mark.parametrize("url", _CONTROL_URLS)
def test_validate_url_scheme_and_configured_url_refuse_control_characters(url: str) -> None:
    _assert_malformed(_refusal(lambda: security_web.validate_url_scheme(url)), "validate_url_scheme")
    _assert_malformed(_refusal(lambda: security_web.validate_configured_url_for_ssrf(url)), "validate_configured_url_for_ssrf")


@pytest.mark.parametrize("url", _CONTROL_URLS)
@pytest.mark.parametrize("origins", [pytest.param((), id="no-origins"), pytest.param(_ORIGINS, id="with-origins")])
def test_validate_allowed_http_origin_refuses_control_characters(url: str, origins: tuple[Any, ...]) -> None:
    _assert_malformed(_refusal(lambda: security_web.validate_allowed_http_origin(url, origins)), "validate_allowed_http_origin")


def test_validate_archived_ssrf_request_refuses_control_characters() -> None:
    url = "https://example.com/\x7f"
    archived = ReplaySSRFRequest(
        original_url=url,
        resolved_ip=_PUBLIC_IP,
        host_header="example.com",
        port=443,
        path="/\x7f",
        scheme="https",
        bare_hostname="example.com",
    )
    _assert_malformed(_refusal(lambda: security_web.validate_archived_ssrf_request(url, archived)), "validate_archived_ssrf_request")


def test_parse_http_origin_refuses_del() -> None:
    refused: ValueError | None = None
    try:
        security_web.parse_http_origin("https://exa\x7fmple.com")
    except ValueError as exc:
        refused = exc
    assert refused is not None


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.com/page", id="plain"),
        pytest.param("https://example.com/a b", id="space-is-printable"),
        pytest.param("https://example.com/café", id="non-ascii-iri"),
        pytest.param("https://bücher.example/", id="idna-host"),
    ],
)
def test_helpers_still_admit_printable_and_non_ascii_urls(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the refusal is exactly httpx's (ASCII and not printable)."""
    _no_dns(monkeypatch)
    assert _refusal(lambda: security_web.validate_url_for_ssrf(url)) is None
    assert _refusal(lambda: security_web.validate_allowed_http_origin(url, ())) is None
    assert _refusal(lambda: security_web.validate_configured_url_for_ssrf(url)) is None


# --- callers inherit the refusal --------------------------------------------


def test_blob_fetch_row_url_with_del_is_a_row_error_and_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.transforms.blob_fetch import BlobFetch

    _no_dns(monkeypatch)
    transform = BlobFetch(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["url"]},
            "url_field": "url",
            "http": {"abuse_contact": "ops@example.com", "fetch_reason": "unit test"},
        }
    )
    fetched: list[object] = []

    def _fetch(safe_request: object, _ctx: object) -> tuple[httpx.Response, str, Call]:
        fetched.append(safe_request)
        return (
            httpx.Response(200, content=b"x", headers={"content-type": "text/csv"}, request=httpx.Request("GET", "https://e.com/")),
            "https://example.com/",
            _Recorder().record_call(),
        )

    monkeypatch.setattr(transform, "_fetch_url", _fetch)
    result = None
    raised: BaseException | None = None
    try:
        result = transform.process(make_pipeline_row({"url": "https://example.com/data\x7f.csv"}), make_context())
    except Exception as exc:
        raised = exc
    assert raised is None, f"process() raised {type(raised).__name__}"
    assert fetched == [], "blob_fetch dispatched a URL containing DEL"
    assert result is not None
    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["cause"] == "malformed_url"


def test_browser_job_refuses_del_in_start_url() -> None:
    from elspeth.core.browser.boundary import BrowserJob, BrowserSecurityError

    refused: BrowserSecurityError | None = None
    try:
        BrowserJob(job_id="row-1", start_url="https://example.com/\x7f", allowed_origins=("https://example.com",))
    except BrowserSecurityError as exc:
        refused = exc
    assert refused is not None


def test_discovered_link_with_del_is_refused() -> None:
    from elspeth.plugins.transforms.web_scrape_links import resolve_discovered_href

    refused: ValueError | None = None
    try:
        resolve_discovered_href("/next\x7f", base_url="https://example.com/", allowed_origins=_ORIGINS, allowed_ranges=())
    except ValueError as exc:
        refused = exc
    assert refused is not None


# --- one shared predicate, matching httpx exactly ---------------------------


def _shared_predicate() -> Callable[[str], bool]:
    namespace = vars(security_web)
    assert "contains_ascii_control_character" in namespace, "core.security.web must export the shared control-character predicate"
    predicate: Callable[[str], bool] = namespace["contains_ascii_control_character"]
    return predicate


def test_shared_control_character_predicate_is_exactly_httpx() -> None:
    predicate = _shared_predicate()
    for code in [*range(128), 0x85, 0xA0, 0x2028, 0xE9, 0x1F600]:
        char = chr(code)
        refused_by_httpx = False
        try:
            httpx.URL(f"https://example.com/{char}")
        except httpx.InvalidURL:
            refused_by_httpx = True
        assert predicate(f"https://example.com/{char}") is refused_by_httpx, f"predicate disagrees with httpx on U+{code:04X}"


@pytest.mark.parametrize("char", ["\x7f", "\x01", "\x1f"])
def test_pagination_link_header_with_control_character_is_refused(char: str) -> None:
    from elspeth.plugins.transforms.web_scrape_pagination import PaginationConfig, discover_next_url

    _shared_predicate()
    response = httpx.Response(200, headers=[("link", f"<https://example.com/p2{char}>; rel=next")])
    refused: ValueError | None = None
    try:
        discover_next_url(response, "https://example.com/", PaginationConfig(mode="link_header"))
    except ValueError as exc:
        refused = exc
    assert refused is not None
