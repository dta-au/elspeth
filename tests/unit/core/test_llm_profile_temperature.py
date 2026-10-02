"""Temperature belongs to operator profiles, never authored profiled nodes."""

import pytest
from pydantic import ValidationError

from elspeth.core.config import _lower_llm_profile_node_options
from elspeth.core.llm_profiles import LLMProfileSettings, RuntimeLLMProfile
from elspeth.web.plugin_policy.profiles import _LLMProfileResolver


@pytest.mark.parametrize("temperature", ["omitted", None, 0, 0.7])
def test_operator_temperature_lowering_preserves_presence_and_web_batch_parity(temperature: object) -> None:
    values = {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku"}
    if temperature != "omitted":
        values["temperature"] = temperature
    settings = LLMProfileSettings.model_validate(values)
    runtime = RuntimeLLMProfile.from_settings("operator", settings)
    resolver = _LLMProfileResolver((("operator", runtime),), preferred_alias="operator")
    safe = {"prompt_template": "hello"}
    web = resolver.lower_options("operator", safe)
    executable, audit = _lower_llm_profile_node_options("operator", settings, {"profile": "operator", **safe})
    assert dict(web.executable_options) == executable
    assert dict(web.audit_safe_options) == audit
    assert "temperature" not in audit
    if temperature == "omitted":
        assert "temperature" not in executable
    else:
        assert executable["temperature"] == temperature


@pytest.mark.parametrize("temperature", [True, False, float("nan"), float("inf"), -0.1, 2.1, "0.7"])
def test_operator_temperature_rejects_invalid_numeric_values(temperature: object) -> None:
    with pytest.raises(ValidationError):
        LLMProfileSettings.model_validate({"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku", "temperature": temperature})


@pytest.mark.parametrize("temperature", [None, 0.0, 0.7])
def test_authored_profile_temperature_is_rejected_in_web_and_batch(temperature: object) -> None:
    settings = LLMProfileSettings(provider="bedrock", model="bedrock/anthropic.claude-3-haiku")
    runtime = RuntimeLLMProfile.from_settings("operator", settings)
    resolver = _LLMProfileResolver((("operator", runtime),), preferred_alias="operator")
    with pytest.raises(ValueError, match="private_profile_option"):
        resolver.lower_options("operator", {"temperature": temperature})
    with pytest.raises(ValueError, match="private_profile_option"):
        _lower_llm_profile_node_options("operator", settings, {"profile": "operator", "temperature": temperature})
