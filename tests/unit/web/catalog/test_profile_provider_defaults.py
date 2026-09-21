"""Operator-owned temperature never appears in profiled authoring schemas."""

import pytest

from elspeth.core.llm_profiles import RuntimeLLMProfile
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.plugin_policy.profiles import _LLMProfileResolver


@pytest.mark.parametrize("kind", ["source", "transform"])
@pytest.mark.parametrize("providers", [("openrouter",), ("azure",), ("azure", "openrouter")])
def test_profile_temperature_is_private_for_every_available_provider(kind: str, providers: tuple[str, ...]) -> None:
    profiles = tuple((provider, RuntimeLLMProfile(alias=provider, provider=provider, model="test")) for provider in ("azure", "openrouter"))
    resolver = _LLMProfileResolver(profiles, preferred_alias=providers[0])
    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    public = resolver.public_schema(catalog.get_schema(kind, "llm"), providers)
    assert "temperature" not in public.json_schema["properties"]
    assert all(field["name"] != "temperature" for field in public.knob_schema["fields"])
    assert "api_key" not in public.json_schema["properties"]
    assert "provider" not in public.json_schema["properties"]
