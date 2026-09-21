"""Azure readiness must agree with the credentials LiteLLM consumes."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.availability import compute_availability
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings


@pytest.mark.parametrize(
    ("provider", "credential", "available"),
    [
        ("azure", "AZURE_API_KEY", True),
        ("azure", "AZURE_OPENAI_API_KEY", True),
        ("azure", "AZURE_AD_TOKEN", True),
        ("azure", "AZURE_AI_API_KEY", False),
        ("azure_ai", "AZURE_AI_API_KEY", True),
        ("azure_ai", "AZURE_API_KEY", False),
    ],
)
@pytest.mark.parametrize("role", ["primary", "advisor"])
def test_azure_readiness_uses_provider_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str, credential: str, available: bool, role: str
) -> None:
    for key in ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_AD_TOKEN", "AZURE_AI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(credential, "test-credential")
    monkeypatch.setenv("OPENAI_API_KEY", "test-other-role-credential")
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model=f"{provider}/deployment" if role == "primary" else "openai/gpt-4o",
        composer_advisor_model=f"{provider}/deployment" if role == "advisor" else "openai/gpt-4o",
        composer_discovery_reasoning_effort="none",
        composer_advisor_reasoning_effort="none",
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=MagicMock(spec=CatalogService), settings=settings)
    result = compute_availability(service)
    assert result.available is available
    if not available:
        assert result.missing_keys == (("AZURE_API_KEY",) if provider == "azure" else ("AZURE_AI_API_KEY",))


def test_litellm_azure_ai_reads_its_own_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm import AzureAIStudioConfig

    monkeypatch.setenv("AZURE_AI_API_BASE", "https://models.example.test")
    monkeypatch.setenv("AZURE_API_KEY", "wrong-provider-credential")
    monkeypatch.setenv("AZURE_AI_API_KEY", "azure-ai-test-credential")
    config = AzureAIStudioConfig()
    assert config._get_openai_compatible_provider_info("deployment", None, None, "azure_ai") == (
        "https://models.example.test",
        "azure-ai-test-credential",
        "azure_ai",
    )
    monkeypatch.delenv("AZURE_AI_API_KEY")
    assert config._get_openai_compatible_provider_info("deployment", None, None, "azure_ai")[1] is None
