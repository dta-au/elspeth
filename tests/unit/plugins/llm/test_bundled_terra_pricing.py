"""The locked SDK must price the production model from its bundled catalog."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.resources import files

import litellm
import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.llm_response_parsing import build_llm_call_record


@pytest.mark.parametrize("model", ["gpt-5.6-terra", "azure/gpt-5.6-terra"])
def test_bundled_catalog_contains_exact_terra_model(model: str) -> None:
    catalog = json.loads(files("litellm").joinpath("model_prices_and_context_window_backup.json").read_text())
    entry = catalog[model]
    assert entry["input_cost_per_token"] == 0.000002
    assert entry["cache_read_input_token_cost"] == 0.0000002
    assert entry["output_cost_per_token"] == 0.000012
    assert entry["input_cost_per_token_above_272k_tokens"] == 0.000004
    assert entry["output_cost_per_token_above_272k_tokens"] == 0.000018
    assert entry["input_cost_per_token_priority"] == 0.000004
    assert entry["output_cost_per_token_priority"] == 0.000024


@pytest.mark.parametrize("model", ["openai/gpt-5.6-terra", "azure/gpt-5.6-terra"])
def test_exact_terra_request_prices_without_returned_alias(model: str) -> None:
    prompt, completion = litellm.cost_per_token(model=model, prompt_tokens=100, completion_tokens=20)
    assert prompt == pytest.approx(0.0002)
    assert completion == pytest.approx(0.00024)


@pytest.mark.parametrize("model", ["openai/gpt-5.6-terra", "azure/gpt-5.6-terra"])
@pytest.mark.parametrize(
    ("prompt_tokens", "cached_tokens", "service_tier", "expected"),
    [(100, 50, None, 0.00035), (300000, 0, None, 1.20036), (100, 0, "priority", 0.00088)],
)
def test_terra_recovered_cost_preserves_pricing_dimensions(
    model: str, prompt_tokens: int, cached_tokens: int, service_tier: str | None, expected: float
) -> None:
    response = litellm.ModelResponse(
        model="gpt-5.6-terra-2026-07-09",
        usage=litellm.Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=20,
            total_tokens=prompt_tokens + 20,
            prompt_tokens_details={"cached_tokens": cached_tokens},
        ),
    )
    response._hidden_params = {}
    if service_tier is not None:
        response.service_tier = service_tier
    record = build_llm_call_record(
        model_requested=model,
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )
    assert record.provider_cost == pytest.approx(expected)
    assert record.provider_cost_source == "litellm.cost_per_token"
    assert record.model_returned == "gpt-5.6-terra-2026-07-09"


def test_fresh_app_import_prices_terra_without_remote_catalog() -> None:
    env = os.environ.copy()
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
from unittest.mock import patch
import httpx
with patch.object(httpx, 'get', side_effect=AssertionError('unexpected pricing egress')):
    import elspeth.web.app
    import litellm
    from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
    info = get_model_cost_map_source_info()
    assert info['source'] == 'local', info
    assert info['is_env_forced'] is True, info
    assert info['fallback_reason'] is None, info
    cost = litellm.cost_per_token(model='openai/gpt-5.6-terra', prompt_tokens=100, completion_tokens=20)
    print(json.dumps(cost))
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout) == pytest.approx([0.0002, 0.00024])
