"""A sessionless deployment probe cannot grant authoring dispatch custody."""

from unittest.mock import AsyncMock

import pytest

from elspeth.web._aws_ecs_acceptance.bedrock import verify_bedrock
from elspeth.web._aws_ecs_acceptance.contracts import AcceptanceCheckError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
async def test_sessionless_bedrock_probe_refuses_without_durable_guard_audit(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    import litellm

    sdk = AsyncMock(side_effect=AssertionError("Sessionless probe entered the protected model SDK"))
    monkeypatch.setattr(litellm, "acompletion", sdk)
    env = {
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/test-model",
        "AWS_REGION": "us-east-1",
        "ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE": mode,
    }
    with pytest.raises(AcceptanceCheckError) as raised:
        await verify_bedrock(env)
    assert raised.value.check == "bedrock_prompt_guard_audit_unavailable"
    sdk.assert_not_awaited()
