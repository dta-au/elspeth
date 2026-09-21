"""Operator billing identities are independent from endpoint routing."""

import pytest
from pydantic import ValidationError

from elspeth.core.llm_profiles import LLMProfileSettings, RuntimeLLMProfile, lower_llm_profile_options


def test_pricing_identity_is_lowered_privately_without_changing_azure_routing() -> None:
    settings = LLMProfileSettings(
        provider="azure",
        model="regional-deployment",
        deployment_name="regional-deployment",
        endpoint="https://example.openai.azure.com",
        credential_scope="server",
        credential_ref="AZURE_API_KEY",
        pricing_model="azure/gpt-4o",
    )
    profile = RuntimeLLMProfile.from_settings("regional", settings)
    executable, audit_safe = lower_llm_profile_options("regional", profile, {"prompt_template": "{{ row.text }}"})

    assert executable["deployment_name"] == "regional-deployment"
    assert executable["pricing_model"] == "azure/gpt-4o"
    assert audit_safe == {"profile": "regional", "prompt_template": "{{ row.text }}"}
    with pytest.raises(ValueError, match="private_profile_option"):
        lower_llm_profile_options("regional", profile, {"pricing_model": "other/model"})


@pytest.mark.parametrize("pricing_model", ["", "   ", "\n", 12, False])
def test_profile_rejects_invalid_pricing_identity(pricing_model: object) -> None:
    with pytest.raises(ValidationError):
        LLMProfileSettings.model_validate(
            {"provider": "bedrock", "model": "anthropic.claude-3-sonnet-20240229-v1:0", "pricing_model": pricing_model}
        )
