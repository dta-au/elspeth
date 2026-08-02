# tests/unit/plugins/llm/test_plugin_registration.py
"""Tests for unified LLM plugin registration and validation dispatch (Task 10).

Verifies that:
- "llm" plugin dispatches to provider-specific config models
- Old plugin names raise helpful migration errors
- Discovery finds the new LLMTransform plugin
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from elspeth.contracts import Determinism
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.validation import get_source_config_model, get_transform_config_model
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.sources.llm.config import (
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    OpenRouterLLMSourceConfig,
)


class TestLLMPluginConfigDispatch:
    """Tests for get_transform_config_model provider dispatch."""

    def test_llm_plugin_dispatches_to_azure_config(self) -> None:
        """verify get_transform_config_model("llm", {"provider": "azure"}) returns AzureOpenAIConfig."""
        from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig

        config_model = get_transform_config_model("llm", {"provider": "azure"})
        assert config_model is AzureOpenAIConfig

    def test_llm_plugin_dispatches_to_openrouter_config(self) -> None:
        """verify get_transform_config_model("llm", {"provider": "openrouter"}) returns OpenRouterConfig."""
        from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig

        config_model = get_transform_config_model("llm", {"provider": "openrouter"})
        assert config_model is OpenRouterConfig

    def test_llm_plugin_dispatches_to_bedrock_config(self) -> None:
        from elspeth.plugins.transforms.llm.providers.bedrock import BedrockConfig

        config_model = get_transform_config_model("llm", {"provider": "bedrock"})
        assert config_model is BedrockConfig

    def test_llm_plugin_missing_provider_falls_back_to_base(self) -> None:
        """verify missing provider key returns LLMConfig (Pydantic catches the Literal validation)."""
        from elspeth.plugins.transforms.llm.base import LLMConfig

        config_model = get_transform_config_model("llm", {})
        assert config_model is LLMConfig


class TestLLMSourcePluginConfigDispatch:
    """Source validation and construction dispatch on the authored provider."""

    @pytest.fixture
    def manager(self, monkeypatch: pytest.MonkeyPatch) -> PluginManager:
        manager = PluginManager()
        manager.register(create_dynamic_hookimpl([LLMSource], "elspeth_get_source"))
        monkeypatch.setattr(
            "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
            lambda: manager,
        )
        return manager

    @pytest.mark.parametrize(
        ("provider_config", "expected_config_model"),
        [
            (
                {
                    "provider": "azure",
                    "deployment_name": "deployment",
                    "endpoint": "https://example.openai.azure.com",
                    "api_key": "resolved-key",
                },
                AzureOpenAILLMSourceConfig,
            ),
            (
                {
                    "provider": "openrouter",
                    "model": "openai/gpt-5-mini",
                    "api_key": "resolved-key",
                },
                OpenRouterLLMSourceConfig,
            ),
            (
                {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "ap-southeast-2",
                },
                BedrockLLMSourceConfig,
            ),
            (
                {
                    "provider": "gateway",
                    "model": "standard",
                    "endpoint": "https://gateway.example.com/v1",
                    "api_key": "resolved-key",
                },
                GatewayLLMSourceConfig,
            ),
        ],
        ids=("azure", "openrouter", "bedrock", "gateway"),
    )
    def test_source_dispatch_and_manager_creation_use_provider_specific_model(
        self,
        manager: PluginManager,
        provider_config: dict[str, object],
        expected_config_model: type,
    ) -> None:
        config = {
            **provider_config,
            "prompt_template": "Write one audit briefing.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }

        assert get_source_config_model("llm", config) is expected_config_model
        source = manager.create_source("llm", config)

        assert isinstance(source, LLMSource)
        assert isinstance(source.provider_config, expected_config_model)

    def test_source_dispatch_rejects_unknown_provider_before_construction(self, manager: PluginManager) -> None:
        config = {
            "provider": "forged",
            "prompt_template": "Write one audit briefing.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }

        with pytest.raises(ValueError, match="Unknown LLM provider 'forged'"):
            manager.create_source("llm", config)

    def test_llm_plugin_unknown_provider_raises(self) -> None:
        """verify unknown provider raises ValueError with valid providers listed."""
        with pytest.raises(ValueError, match="Unknown LLM provider 'fake'"):
            get_transform_config_model("llm", {"provider": "fake"})

    def test_llm_plugin_none_config_falls_back_to_base(self) -> None:
        """verify None config falls back to LLMConfig."""
        from elspeth.plugins.transforms.llm.base import LLMConfig

        config_model = get_transform_config_model("llm", None)
        assert config_model is LLMConfig


class TestOldPluginNamesRejected:
    """Old plugin names are rejected as unknown types."""

    @pytest.mark.parametrize(
        "old_name",
        ["azure_llm", "openrouter_llm", "azure_multi_query_llm", "openrouter_multi_query_llm"],
    )
    def test_old_plugin_names_raise_unknown_type(self, old_name: str) -> None:
        """Old plugin names should raise ValueError as unknown transform type."""
        with pytest.raises(ValueError, match="Unknown transform type"):
            get_transform_config_model(old_name)


class TestLLMPluginDiscovery:
    """Verify the unified LLMTransform is discovered correctly."""

    def test_llm_plugin_discovered_with_correct_name(self) -> None:
        from elspeth.plugins.infrastructure.discovery import discover_all_plugins

        discovered = discover_all_plugins()
        transform_names = [cls.name for cls in discovered["transforms"]]  # type: ignore[attr-defined]
        assert "llm" in transform_names

    def test_llm_plugin_is_non_deterministic(self) -> None:
        from elspeth.plugins.transforms.llm.transform import LLMTransform

        assert LLMTransform.determinism == Determinism.NON_DETERMINISTIC

    def test_llm_source_is_discovered_by_shared_manager_with_canonical_identity(self) -> None:
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        assert get_shared_plugin_manager().get_source_by_name("llm") is LLMSource

    def test_fresh_process_resolves_canonical_llm_source_identity(self) -> None:
        repository_root = Path(__file__).resolve().parents[4]
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(repository_root / "src"), str(repository_root / "elspeth-lints" / "src"))),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager; "
                    "from elspeth.plugins.sources.llm.source import LLMSource; "
                    "assert get_shared_plugin_manager().get_source_by_name('llm') is LLMSource; "
                    "assert LLMSource.__module__ == 'elspeth.plugins.sources.llm.source'"
                ),
            ],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
