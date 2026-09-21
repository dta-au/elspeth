"""Profile catalog defaults must match the selected runtime providers."""

import pytest

from elspeth.core.llm_profiles import RuntimeLLMProfile
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.plugin_policy.profiles import _LLMProfileResolver


@pytest.mark.parametrize("kind", ["source", "transform"])
@pytest.mark.parametrize("providers,expected", [(("openrouter",), 0.0), (("azure",), None), (("azure", "openrouter"), "absent")])
def test_profile_temperature_default_matches_available_providers(kind: str, providers: tuple[str, ...], expected: object) -> None:
    profiles = tuple((provider, RuntimeLLMProfile(alias=provider, provider=provider, model="test")) for provider in ("azure", "openrouter"))
    resolver = _LLMProfileResolver(profiles, preferred_alias=providers[0])
    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    public = resolver.public_schema(catalog.get_schema(kind, "llm"), providers)
    temperature = public.json_schema["properties"]["temperature"]
    if expected == "absent":
        assert "default" not in temperature
        assert "description" not in temperature
    else:
        assert temperature["default"] == expected
    if kind == "source":
        field = next(field for field in public.knob_schema["fields"] if field["name"] == "temperature")
        if expected == "absent":
            assert "default" not in field
        else:
            assert field["default"] == expected
    assert "api_key" not in public.json_schema["properties"]
    assert "provider" not in public.json_schema["properties"]
