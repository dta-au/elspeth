# tests/fixtures/chaosweb.py
"""ChaosWeb TestClient fixture for testing web scraping pipeline resilience.

Provides a pytest fixture that creates a ChaosWeb server in-process using
Starlette's TestClient (no real network socket — safe for parallel tests).

Usage:
    # In conftest.py:
    from tests.fixtures.chaosweb import chaosweb_server  # noqa: F401

    # In tests:
    def test_scraper(chaosweb_server):
        response = chaosweb_server.fetch_page("/articles/test")
        assert response.status_code == 200

    # With marker overrides:
    @pytest.mark.chaosweb(preset="stress_scraping", rate_limit_pct=25.0)
    def test_under_stress(chaosweb_server):
        ...
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from errorworks.web.config import ChaosWebConfig, load_config
from errorworks.web.server import ChaosWebServer
from starlette.testclient import TestClient

if TYPE_CHECKING:
    import httpx


@dataclass
class ChaosWebFixture:
    """Pytest fixture object for ChaosWeb server."""

    client: TestClient
    server: ChaosWebServer
    metrics_db_path: Path
    _request_count: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def base_url(self) -> str:
        return "http://testserver"

    @property
    def port(self) -> int:
        return 8200

    @property
    def metrics_db(self) -> Path:
        return self.metrics_db_path

    @property
    def admin_url(self) -> str:
        return f"{self.base_url}/admin"

    @property
    def run_id(self) -> str:
        return self.server.run_id

    def fetch_page(
        self,
        path: str = "/",
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """GET a page from the ChaosWeb server.

        Args:
            path: URL path to fetch (e.g., "/articles/test").
            headers: Additional HTTP headers.
            follow_redirects: Whether to follow 3xx redirects.

        Returns:
            httpx.Response with status, headers, and body.
        """
        return self.client.get(
            path,
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def get_stats(self) -> dict[str, Any]:
        return self.server.get_stats()

    def export_metrics(self) -> dict[str, Any]:
        return self.server.export_metrics()

    def update_config(
        self,
        *,
        # HTTP-level errors
        rate_limit_pct: float | None = None,
        forbidden_pct: float | None = None,
        not_found_pct: float | None = None,
        gone_pct: float | None = None,
        payment_required_pct: float | None = None,
        unavailable_for_legal_pct: float | None = None,
        service_unavailable_pct: float | None = None,
        bad_gateway_pct: float | None = None,
        gateway_timeout_pct: float | None = None,
        internal_error_pct: float | None = None,
        # Connection-level failures
        timeout_pct: float | None = None,
        connection_reset_pct: float | None = None,
        connection_stall_pct: float | None = None,
        slow_response_pct: float | None = None,
        incomplete_response_pct: float | None = None,
        # Content malformations
        wrong_content_type_pct: float | None = None,
        encoding_mismatch_pct: float | None = None,
        truncated_html_pct: float | None = None,
        invalid_encoding_pct: float | None = None,
        charset_confusion_pct: float | None = None,
        malformed_meta_pct: float | None = None,
        # Redirect injection
        redirect_loop_pct: float | None = None,
        ssrf_redirect_pct: float | None = None,
        # Selection and latency
        selection_mode: str | None = None,
        base_ms: int | None = None,
        jitter_ms: int | None = None,
        # Content mode
        content_mode: str | None = None,
    ) -> None:
        """Update runtime configuration for the server.

        Any parameters not provided (None) are left unchanged.
        """
        updates: dict[str, Any] = {}
        error_updates: dict[str, float | str] = {}
        for key, val in [
            ("rate_limit_pct", rate_limit_pct),
            ("forbidden_pct", forbidden_pct),
            ("not_found_pct", not_found_pct),
            ("gone_pct", gone_pct),
            ("payment_required_pct", payment_required_pct),
            ("unavailable_for_legal_pct", unavailable_for_legal_pct),
            ("service_unavailable_pct", service_unavailable_pct),
            ("bad_gateway_pct", bad_gateway_pct),
            ("gateway_timeout_pct", gateway_timeout_pct),
            ("internal_error_pct", internal_error_pct),
            ("timeout_pct", timeout_pct),
            ("connection_reset_pct", connection_reset_pct),
            ("connection_stall_pct", connection_stall_pct),
            ("slow_response_pct", slow_response_pct),
            ("incomplete_response_pct", incomplete_response_pct),
            ("wrong_content_type_pct", wrong_content_type_pct),
            ("encoding_mismatch_pct", encoding_mismatch_pct),
            ("truncated_html_pct", truncated_html_pct),
            ("invalid_encoding_pct", invalid_encoding_pct),
            ("charset_confusion_pct", charset_confusion_pct),
            ("malformed_meta_pct", malformed_meta_pct),
            ("redirect_loop_pct", redirect_loop_pct),
            ("ssrf_redirect_pct", ssrf_redirect_pct),
            ("selection_mode", selection_mode),
        ]:
            if val is not None:
                error_updates[key] = val
        if error_updates:
            updates["error_injection"] = error_updates

        latency_updates: dict[str, int] = {}
        if base_ms is not None:
            latency_updates["base_ms"] = base_ms
        if jitter_ms is not None:
            latency_updates["jitter_ms"] = jitter_ms
        if latency_updates:
            updates["latency"] = latency_updates

        content_updates: dict[str, str] = {}
        if content_mode is not None:
            content_updates["mode"] = content_mode
        if content_updates:
            updates["content"] = content_updates

        if updates:
            self.server.update_config(updates)

    def reset(self) -> str:
        return self.server.reset()

    def wait_for_requests(self, count: int, timeout: float = 10.0) -> bool:
        """Block until at least `count` requests have been recorded.

        Returns True if the count was reached, False on timeout.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            stats = self.get_stats()
            if stats["total_requests"] >= count:
                return True
            time.sleep(0.01)
        return False


# All error injection fields on WebErrorInjectionConfig that are float percentages.
_ERROR_INJECTION_KEYS = [
    "rate_limit_pct",
    "forbidden_pct",
    "not_found_pct",
    "gone_pct",
    "payment_required_pct",
    "unavailable_for_legal_pct",
    "service_unavailable_pct",
    "bad_gateway_pct",
    "gateway_timeout_pct",
    "internal_error_pct",
    "timeout_pct",
    "connection_reset_pct",
    "connection_stall_pct",
    "slow_response_pct",
    "incomplete_response_pct",
    "wrong_content_type_pct",
    "encoding_mismatch_pct",
    "truncated_html_pct",
    "invalid_encoding_pct",
    "charset_confusion_pct",
    "malformed_meta_pct",
    "redirect_loop_pct",
    "ssrf_redirect_pct",
    "selection_mode",
]


def _build_config_from_marker(
    marker: pytest.Mark | None,
    tmp_path: Path,
) -> ChaosWebConfig:
    """Build ChaosWebConfig from pytest marker kwargs."""
    metrics_db_path = tmp_path / "chaosweb-metrics.db"
    base_config: dict[str, Any] = {
        "metrics": {"database": str(metrics_db_path)},
        "latency": {"base_ms": 0, "jitter_ms": 0},
    }

    if marker is None:
        return ChaosWebConfig(**base_config)

    if marker.args:
        raise pytest.UsageError("chaosweb marker does not accept positional arguments; use keyword arguments")
    allowed_keys = {*_ERROR_INJECTION_KEYS, "base_ms", "jitter_ms", "content_mode", "preset"}
    unknown_keys = marker.kwargs.keys() - allowed_keys
    if unknown_keys:
        raise pytest.UsageError(f"Unknown chaosweb marker argument(s): {', '.join(sorted(unknown_keys))}")

    preset = marker.kwargs.get("preset")
    overrides: dict[str, Any] = {}

    # Error injection overrides from marker kwargs
    error_overrides: dict[str, float | str] = {}
    for key in _ERROR_INJECTION_KEYS:
        if key in marker.kwargs:
            error_overrides[key] = marker.kwargs[key]
    if error_overrides:
        overrides["error_injection"] = error_overrides

    # Latency overrides
    latency_overrides: dict[str, int] = {}
    if "base_ms" in marker.kwargs:
        latency_overrides["base_ms"] = marker.kwargs["base_ms"]
    if "jitter_ms" in marker.kwargs:
        latency_overrides["jitter_ms"] = marker.kwargs["jitter_ms"]
    if latency_overrides:
        overrides["latency"] = latency_overrides

    # Content mode override
    if "content_mode" in marker.kwargs:
        overrides["content"] = {"mode": marker.kwargs["content_mode"]}

    if preset or overrides:
        return load_config(
            preset=preset,
            cli_overrides={**base_config, **overrides} if overrides else base_config,
        )

    return ChaosWebConfig(**base_config)


@pytest.fixture
def chaosweb_server(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[ChaosWebFixture, None, None]:
    """Create a ChaosWeb fake web server for testing.

    Usage:
        def test_pipeline(chaosweb_server):
            response = chaosweb_server.fetch_page("/articles/test")
            assert response.status_code == 200

        @pytest.mark.chaosweb(preset="stress_scraping", rate_limit_pct=25.0)
        def test_under_stress(chaosweb_server):
            ...
    """
    marker = request.node.get_closest_marker("chaosweb")
    config = _build_config_from_marker(marker, tmp_path)
    server = ChaosWebServer(config)
    client = TestClient(server.app)
    metrics_db_path = Path(config.metrics.database)
    fixture = ChaosWebFixture(client=client, server=server, metrics_db_path=metrics_db_path)
    yield fixture
    client.close()
