"""Observe real OpenRouter HTTP dispatches without changing their request bytes."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from unittest.mock import patch

import httpx


class ProviderBudgetExceeded(RuntimeError):
    """The next physical provider request would exceed the acceptance budget."""


@dataclass
class ProviderBudget:
    path: Path
    max_calls: int = 200
    max_cost: float = 10.0
    calls: int = 0
    cost: float = 0.0
    unpriced: int = 0
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        if self.max_calls < 1 or self.max_cost <= 0:
            raise ValueError("Provider budgets must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            starts: set[int] = set()
            priced: set[int] = set()
            for line in self.path.read_text().splitlines():
                row = json.loads(line)
                if row["event"] == "dispatch":
                    starts.add(row["ordinal"])
                elif row["event"] == "result" and row["cost"] is not None:
                    self.cost += row["cost"]
                    priced.add(row["ordinal"])
            self.calls = len(starts)
            self.unpriced = len(starts - priced)

    def _write(self, row: dict[str, Any]) -> None:
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")

    def reserve(self) -> int:
        with self._lock:
            if self.calls >= self.max_calls or self.cost >= self.max_cost:
                raise ProviderBudgetExceeded("Global acceptance provider budget exhausted")
            self.calls += 1
            self.unpriced += 1
            self._write({"event": "dispatch", "ordinal": self.calls, "at": datetime.now(UTC).isoformat()})
            return self.calls

    def finish(self, ordinal: int, *, payload: object, status: int | None, error: str | None, elapsed: float | None = None) -> None:
        cost = None
        model = None
        provider = None
        request_id = None
        if isinstance(payload, dict):
            usage = payload.get("usage")
            if isinstance(usage, dict):
                candidate = usage.get("cost")
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and math.isfinite(candidate) and candidate >= 0:
                    cost = float(candidate)
            for name in ("model", "provider", "id"):
                value = payload.get(name)
                if value is not None and not isinstance(value, str):
                    raise ValueError("Provider response identity must be a string")
            model, provider, request_id = payload.get("model"), payload.get("provider"), payload.get("id")
        with self._lock:
            if cost is not None:
                self.cost += cost
                self.unpriced -= 1
            self._write(
                {
                    "event": "result",
                    "ordinal": ordinal,
                    "http_status": status,
                    "cost": cost,
                    "known_cost_total": self.cost,
                    "unpriced_calls": self.unpriced,
                    "error_class": error,
                    "elapsed_seconds": elapsed,
                    "model": model,
                    "provider": provider,
                    "request_id": request_id,
                    "cost_overshoot": max(0.0, self.cost - self.max_cost),
                }
            )


def _is_provider_request(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.host == "openrouter.ai" and request.url.path.endswith("/chat/completions")


def _response_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        return None


@contextmanager
def observe_provider_requests(budget: ProviderBudget) -> Iterator[None]:
    """Count both LiteLLM async requests and runtime plugin sync requests."""
    original_sync = httpx.Client.send
    original_async = httpx.AsyncClient.send

    def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if not _is_provider_request(request):
            return original_sync(client, request, **kwargs)
        ordinal = budget.reserve()
        started = time.monotonic()
        try:
            response = original_sync(client, request, **kwargs)
            response.read()
        except BaseException as exc:
            budget.finish(ordinal, payload=None, status=None, error=type(exc).__name__, elapsed=time.monotonic() - started)
            raise
        budget.finish(
            ordinal, payload=_response_payload(response), status=response.status_code, error=None, elapsed=time.monotonic() - started
        )
        return response

    async def send_async(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if not _is_provider_request(request):
            return await original_async(client, request, **kwargs)
        ordinal = budget.reserve()
        started = time.monotonic()
        try:
            response = await original_async(client, request, **kwargs)
            await response.aread()
        except BaseException as exc:
            budget.finish(ordinal, payload=None, status=None, error=type(exc).__name__, elapsed=time.monotonic() - started)
            raise
        budget.finish(
            ordinal, payload=_response_payload(response), status=response.status_code, error=None, elapsed=time.monotonic() - started
        )
        return response

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.Client, "send", send))
        stack.enter_context(patch.object(httpx.AsyncClient, "send", send_async))
        yield
