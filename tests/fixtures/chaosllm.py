# tests/fixtures/chaosllm.py
"""ChaosLLM TestClient fixture for testing LLM error injection.

Migrated from tests/fixtures/chaosllm.py — standalone, no v1 imports.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from errorworks.llm.config import (
    ChaosLLMConfig,
    load_config,
)
from errorworks.llm.server import ChaosLLMServer
from starlette.testclient import TestClient

if TYPE_CHECKING:
    import httpx


@dataclass
class ChaosLLMFixture:
    """Pytest fixture object for ChaosLLM server."""

    client: TestClient
    server: ChaosLLMServer
    metrics_db_path: Path
    _request_count: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def url(self) -> str:
        return "http://testserver"

    @property
    def port(self) -> int:
        return 8000

    @property
    def metrics_db(self) -> Path:
        return self.metrics_db_path

    @property
    def admin_url(self) -> str:
        return f"{self.url}/admin"

    @property
    def run_id(self) -> str:
        return self.server.run_id

    def get_stats(self) -> dict[str, Any]:
        return self.server.get_stats()

    def export_metrics(self) -> dict[str, Any]:
        return self.server.export_metrics()

    def update_config(
        self,
        *,
        rate_limit_pct: float | None = None,
        capacity_529_pct: float | None = None,
        service_unavailable_pct: float | None = None,
        bad_gateway_pct: float | None = None,
        gateway_timeout_pct: float | None = None,
        internal_error_pct: float | None = None,
        timeout_pct: float | None = None,
        connection_reset_pct: float | None = None,
        slow_response_pct: float | None = None,
        invalid_json_pct: float | None = None,
        truncated_pct: float | None = None,
        empty_body_pct: float | None = None,
        missing_fields_pct: float | None = None,
        wrong_content_type_pct: float | None = None,
        selection_mode: str | None = None,
        base_ms: int | None = None,
        jitter_ms: int | None = None,
        mode: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        error_updates: dict[str, float | str] = {}
        for key, val in [
            ("rate_limit_pct", rate_limit_pct),
            ("capacity_529_pct", capacity_529_pct),
            ("service_unavailable_pct", service_unavailable_pct),
            ("bad_gateway_pct", bad_gateway_pct),
            ("gateway_timeout_pct", gateway_timeout_pct),
            ("internal_error_pct", internal_error_pct),
            ("timeout_pct", timeout_pct),
            ("connection_reset_pct", connection_reset_pct),
            ("slow_response_pct", slow_response_pct),
            ("invalid_json_pct", invalid_json_pct),
            ("truncated_pct", truncated_pct),
            ("empty_body_pct", empty_body_pct),
            ("missing_fields_pct", missing_fields_pct),
            ("wrong_content_type_pct", wrong_content_type_pct),
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

        response_updates: dict[str, str] = {}
        if mode is not None:
            response_updates["mode"] = mode
        if response_updates:
            updates["response"] = response_updates

        if updates:
            self.server.update_config(updates)

    def reset(self) -> str:
        return self.server.reset()

    def wait_for_requests(self, count: int, timeout: float = 10.0) -> bool:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            stats = self.get_stats()
            if stats["total_requests"] >= count:
                return True
            time.sleep(0.01)
        return False

    def post_completion(
        self,
        messages: list[dict[str, str]] | None = None,
        model: str = "gpt-4",
        **kwargs: Any,
    ) -> httpx.Response:
        if messages is None:
            messages = [{"role": "user", "content": "Hello"}]
        body = {"model": model, "messages": messages, **kwargs}
        return self.client.post("/v1/chat/completions", json=body)

    def post_azure_completion(
        self,
        deployment: str,
        messages: list[dict[str, str]] | None = None,
        api_version: str = "2024-02-01",
        **kwargs: Any,
    ) -> httpx.Response:
        if messages is None:
            messages = [{"role": "user", "content": "Hello"}]
        body = {"messages": messages, **kwargs}
        url = f"/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        return self.client.post(url, json=body)


_ERROR_INJECTION_KEYS = (
    "rate_limit_pct",
    "capacity_529_pct",
    "service_unavailable_pct",
    "bad_gateway_pct",
    "gateway_timeout_pct",
    "internal_error_pct",
    "timeout_pct",
    "connection_reset_pct",
    "slow_response_pct",
    "invalid_json_pct",
    "truncated_pct",
    "empty_body_pct",
    "missing_fields_pct",
    "wrong_content_type_pct",
    "selection_mode",
)


def _build_config_from_marker(
    marker: pytest.Mark | None,
    tmp_path: Path,
) -> ChaosLLMConfig:
    metrics_db_path = tmp_path / "chaosllm-metrics.db"
    base_config: dict[str, Any] = {
        "metrics": {"database": str(metrics_db_path)},
        "latency": {"base_ms": 0, "jitter_ms": 0},
    }

    if marker is None:
        return ChaosLLMConfig(**base_config)

    if marker.args:
        raise pytest.UsageError("chaosllm marker does not accept positional arguments; use keyword arguments")
    allowed_keys = {*_ERROR_INJECTION_KEYS, "base_ms", "jitter_ms", "mode", "preset"}
    unknown_keys = marker.kwargs.keys() - allowed_keys
    if unknown_keys:
        raise pytest.UsageError(f"Unknown chaosllm marker argument(s): {', '.join(sorted(unknown_keys))}")

    preset = marker.kwargs.get("preset")
    overrides: dict[str, Any] = {}

    error_overrides: dict[str, float | str] = {}
    for key in _ERROR_INJECTION_KEYS:
        if key in marker.kwargs:
            error_overrides[key] = marker.kwargs[key]
    if error_overrides:
        overrides["error_injection"] = error_overrides

    latency_overrides: dict[str, int] = {}
    if "base_ms" in marker.kwargs:
        latency_overrides["base_ms"] = marker.kwargs["base_ms"]
    if "jitter_ms" in marker.kwargs:
        latency_overrides["jitter_ms"] = marker.kwargs["jitter_ms"]
    if latency_overrides:
        overrides["latency"] = latency_overrides

    if "mode" in marker.kwargs:
        overrides["response"] = {"mode": marker.kwargs["mode"]}

    if preset or overrides:
        return load_config(
            preset=preset,
            cli_overrides={**base_config, **overrides} if overrides else base_config,
        )

    return ChaosLLMConfig(**base_config)


@pytest.fixture
def chaosllm_server(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[ChaosLLMFixture, None, None]:
    """Create a ChaosLLM fake LLM server for testing."""
    marker = request.node.get_closest_marker("chaosllm")
    config = _build_config_from_marker(marker, tmp_path)
    server = ChaosLLMServer(config)
    client = TestClient(server.app)
    metrics_db_path = Path(config.metrics.database)
    fixture = ChaosLLMFixture(client=client, server=server, metrics_db_path=metrics_db_path)
    yield fixture
    client.close()
