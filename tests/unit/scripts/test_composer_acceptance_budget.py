"""Provider-free controls for physical dispatch accounting in the live battery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from scripts.composer_acceptance.budget import ProviderBudget, ProviderBudgetExceeded, observe_provider_requests


def test_sync_async_dispatches_share_budget_and_preserve_bytes(tmp_path: Path) -> None:
    observed = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request.content)
        return httpx.Response(200, json={"id": "request-1", "model": "test-model", "provider": "test-route", "usage": {"cost": 0.25}})

    budget = ProviderBudget(tmp_path / "ledger.jsonl", max_calls=2, max_cost=10)
    payload = b'{"model":"test-model","messages":[]}'
    with observe_provider_requests(budget):
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            client.post("https://openrouter.ai/api/v1/chat/completions", content=payload)

        async def second() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await client.post("https://openrouter.ai/api/v1/chat/completions", content=payload)

        asyncio.run(second())
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ProviderBudgetExceeded):
                client.post("https://openrouter.ai/api/v1/chat/completions", content=payload)
            client.get("https://openrouter.ai/api/v1/models")
    assert observed == [payload, payload, b""]
    assert budget.calls == 2
    assert budget.cost == 0.5
    assert budget.unpriced == 0
    restored = ProviderBudget(tmp_path / "ledger.jsonl", max_calls=2, max_cost=10)
    assert (restored.calls, restored.cost, restored.unpriced) == (2, 0.5, 0)


def test_cost_stop_is_after_response_and_records_overshoot(tmp_path: Path) -> None:
    budget = ProviderBudget(tmp_path / "ledger.jsonl", max_calls=10, max_cost=1)
    first = budget.reserve()
    budget.finish(first, payload={"usage": {"cost": 1.2}}, status=200, error=None)
    with pytest.raises(ProviderBudgetExceeded):
        budget.reserve()
    last = json.loads(budget.path.read_text().splitlines()[-1])
    assert last["cost_overshoot"] == pytest.approx(0.2)
    assert budget.calls == 1


def test_unpriced_failures_still_consume_physical_calls(tmp_path: Path) -> None:
    budget = ProviderBudget(tmp_path / "ledger.jsonl", max_calls=1)
    first = budget.reserve()
    budget.finish(first, payload=None, status=503, error="HTTPStatusError")
    assert budget.unpriced == 1
    with pytest.raises(ProviderBudgetExceeded):
        budget.reserve()
    restored = ProviderBudget(budget.path, max_calls=1)
    assert restored.unpriced == 1
