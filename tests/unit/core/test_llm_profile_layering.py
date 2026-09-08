"""Profile validation consumes provider policies without loading plugin runtime."""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from elspeth.core.llm_profiles import LLMProfileSettings
from elspeth.core.llm_provider_validation import LLM_PROVIDER_NAMES


def test_profile_admission_does_not_load_plugin_runtime() -> None:
    # A fresh interpreter detects import coupling even when another test has
    # already initialized the plugin registry in this pytest worker.
    script = """
import sys
from elspeth.core.llm_profiles import LLMProfileSettings

common = dict(model='model', credential_scope='server', credential_ref='TEST_KEY')
LLMProfileSettings(provider='openrouter', **common)
LLMProfileSettings(provider='azure', endpoint='https://example.azure.com', deployment_name='model', **common)
LLMProfileSettings(provider='bedrock', model='bedrock/model', region_name='ap-southeast-2')
LLMProfileSettings(provider='gateway', endpoint='http://127.0.0.1:8787/v1', contract_major=1,
                   required_capabilities=('text',), **common)
assert not any(name == 'elspeth.plugins' or name.startswith('elspeth.plugins.') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=30)


def test_profile_provider_contract_matches_implemented_variants() -> None:
    from elspeth.plugins.transforms.llm.transform import LLMTransform

    assert LLMTransform.discriminated_variants()[1].keys() == LLM_PROVIDER_NAMES


@pytest.mark.parametrize("model", ["bedrock/", "model", " bedrock/model", "bedrock/model\n", "bedrock/mo\x7fdel"])
def test_profile_rejects_invalid_bedrock_model(model: str) -> None:
    with pytest.raises(ValidationError, match="Bedrock model"):
        LLMProfileSettings(provider="bedrock", model=model)


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "http://localhost:8787/v1"},
        {"endpoint": "https://example.com/v1?mode=other"},
        {"endpoint": "https://example.com/v1#other"},
        {"endpoint": "https://example.com/v1/../v1"},
        {"endpoint": "https://example.com/v2"},
        {"contract_major": 2},
        {"required_capabilities": ("unsupported",)},
        {"required_capabilities": ("text", "text")},
    ],
)
def test_profile_rejects_invalid_gateway_binding(changes: dict[str, object]) -> None:
    config = {
        "provider": "gateway",
        "model": "model",
        "credential_scope": "server",
        "credential_ref": "TEST_KEY",
        "endpoint": "http://127.0.0.1:8787/v1",
        "contract_major": 1,
        "required_capabilities": ("text",),
    }
    config.update(changes)
    with pytest.raises(ValidationError):
        LLMProfileSettings.model_validate(config)
