"""Rejected HTTP completions retain observed usage in the real audit store."""

import httpx
import pytest
import respx
from sqlalchemy import select

from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.token_usage import TokenUsage
from elspeth.core.landscape.schema import calls_table
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterLLMProvider
from tests.fixtures.landscape import leader_coordination_token
from tests.unit.core.landscape.test_call_recording import _setup_with_operation


@pytest.mark.parametrize("gateway", [False, True])
@pytest.mark.parametrize("header", ["1", "2"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"error":{"code":"upstream_unavailable"},"usage":{"prompt_tokens":17,"completion_tokens":9,"prompt_tokens_details":{"cached_tokens":3},"completion_tokens_details":{"reasoning_tokens":2}}}',
            TokenUsage(prompt_tokens=17, completion_tokens=9, cached_prompt_tokens=3, reasoning_tokens=2),
        ),
        ('{"error":{"code":"upstream_unavailable"}}', TokenUsage()),
        ('{"usage":{"prompt_tokens":true,"completion_tokens":-1,"cached_prompt_tokens":"3"}}', TokenUsage()),
        ("not JSON", TokenUsage()),
    ],
    ids=["reported", "absent", "invalid-counters", "invalid-json"],
)
@respx.mock
def test_http_error_usage_reaches_database(gateway: bool, header: str, body: str, expected: TokenUsage) -> None:
    db, factory, _, operation_id = _setup_with_operation()
    token = leader_coordination_token(factory, "run-1")
    endpoint = "http://127.0.0.1:8765/v1"
    provider = (
        GatewayLLMProvider(
            endpoint=endpoint,
            api_key="test",
            contract_major=1,
            recorder=factory.execution,
            run_id="run-1",
            telemetry_emit=lambda event: None,
        )
        if gateway
        else OpenRouterLLMProvider(
            base_url=endpoint,
            api_key="test",
            timeout_seconds=30,
            recorder=factory.execution,
            run_id="run-1",
            telemetry_emit=lambda event: None,
        )
    )
    respx.post(f"{endpoint}/chat/completions").mock(
        return_value=httpx.Response(503, content=body.encode(), headers={"X-ELSPETH-LLM-Gateway-Contract": header})
    )
    try:
        with pytest.raises(LLMClientError):
            provider.execute_query(
                messages=[ChatMessage(role="user", content="hi")],
                model="standard",
                temperature=0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_operation(coordination_token=token, operation_id=operation_id),
            )
        with db.read_only_connection() as conn:
            rows = conn.execute(select(calls_table)).all()
        logical = [row for row in rows if row.call_type == "llm"]
        assert len(logical) == 1
        row = logical[0]
        assert row.status == "error"
        assert (row.prompt_tokens, row.completion_tokens, row.cached_prompt_tokens, row.reasoning_tokens) == (
            expected.prompt_tokens,
            expected.completion_tokens,
            expected.cached_prompt_tokens,
            expected.reasoning_tokens,
        )
        transport = [row for row in rows if row.call_type == "http"]
        assert len(transport) == 1
        assert transport[0].prompt_tokens is None
        assert transport[0].completion_tokens is None
    finally:
        provider.close()
        db.close()
