"""Tests for web-authored provider configuration policy helpers."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.composer.tools._common import (
    _PLUGIN_UNAVAILABLE_EXPLANATIONS,
    _plugin_unavailable_message,
)
from elspeth.web.plugin_policy.models import PluginUnavailableReason
from elspeth.web.provider_config_policy import (
    AWS_S3_ENDPOINT_URL_POLICY_ERROR,
    AWS_S3_SOURCE_POLICY_ERROR,
    LLM_BASE_URL_POLICY_ERROR,
    LLM_TRACING_POLICY_ERROR,
    web_aws_s3_endpoint_url_policy_error,
    web_aws_s3_source_policy_error,
    web_llm_base_url_policy_error,
    web_llm_tracing_policy_error,
)


class TestWebLlmBaseUrlPolicy:
    def test_malformed_url_is_rejected_without_reflecting_it(self) -> None:
        malformed_canary = "https://[BASE_URL_CANARY"

        error = web_llm_base_url_policy_error("llm", {"base_url": malformed_canary})

        assert error == LLM_BASE_URL_POLICY_ERROR
        assert "BASE_URL_CANARY" not in error


class TestWebAwsS3SourcePolicy:
    @pytest.mark.parametrize("plugin", ["csv", None, "json"])
    def test_allows_non_aws_s3_sources(self, plugin: str | None) -> None:
        assert web_aws_s3_source_policy_error(plugin, operator_profile_available=False) is None

    def test_rejects_aws_s3_source_without_operator_profile(self) -> None:
        error = web_aws_s3_source_policy_error("aws_s3", operator_profile_available=False)

        assert error == AWS_S3_SOURCE_POLICY_ERROR
        assert "prod-confidential-bucket" not in error
        assert "payroll/ssn.csv" not in error

    def test_allows_aws_s3_source_with_operator_profile(self) -> None:
        assert web_aws_s3_source_policy_error("aws_s3", operator_profile_available=True) is None


class TestWebSurfaceProhibitionExplanation:
    """A generic snapshot prohibition must arrive as plain language."""

    def test_every_unavailable_reason_has_an_explanation(self) -> None:
        """A reason without an entry raises KeyError mid-tool-call.

        The lookup in ``_plugin_unavailable_message`` is deliberately total, so
        adding an enum member without its copy turns an honest decline into a
        500. This pins the pair together.
        """
        assert set(_PLUGIN_UNAVAILABLE_EXPLANATIONS) == set(PluginUnavailableReason)

    def test_prohibition_message_carries_the_policy_reason_and_its_cause(self) -> None:
        message = _plugin_unavailable_message("source", PluginUnavailableReason.WEB_SURFACE_PROHIBITED)

        assert PluginUnavailableReason.WEB_SURFACE_PROHIBITED.value in message
        assert "prohibited on the web authoring surface" in message
        assert "aws_s3" not in message
        assert "no operator setting can enable it here" not in message


class TestWebAwsS3EndpointUrlPolicy:
    @pytest.mark.parametrize(
        ("plugin", "options"),
        [
            ("csv", {"endpoint_url": "http://127.0.0.1:9000"}),
            (None, {"endpoint_url": "http://127.0.0.1:9000"}),
            ("aws_s3", {}),
            ("aws_s3", {"endpoint_url": None}),
        ],
    )
    def test_allows_non_aws_or_absent_endpoint(
        self,
        plugin: str | None,
        options: dict[str, Any],
    ) -> None:
        assert web_aws_s3_endpoint_url_policy_error(plugin, options) is None

    @pytest.mark.parametrize(
        "endpoint_url",
        [
            "http://127.0.0.1:9000",
            "https://storage.attacker.invalid",
            17,
        ],
    )
    def test_rejects_every_non_null_endpoint_without_echoing_it(self, endpoint_url: object) -> None:
        error = web_aws_s3_endpoint_url_policy_error("aws_s3", {"endpoint_url": endpoint_url})

        assert error == AWS_S3_ENDPOINT_URL_POLICY_ERROR
        assert str(endpoint_url) not in error


class TestWebLlmTracingPolicy:
    @pytest.mark.parametrize(
        ("plugin", "options"),
        [
            ("csv", {"tracing": {"host": "https://attacker.invalid"}}),
            (None, {"tracing": {"host": "https://attacker.invalid"}}),
            ("llm", {}),
            ("llm", {"tracing": None}),
        ],
    )
    def test_allows_non_llm_or_absent_tracing(self, plugin: str | None, options: dict[str, Any]) -> None:
        assert web_llm_tracing_policy_error(plugin, options) is None

    @pytest.mark.parametrize("tracing", [{}, False, "langfuse", {"host": "https://credential-canary.attacker.invalid"}])
    def test_rejects_every_non_null_tracing_value_without_echoing_it(self, tracing: object) -> None:
        error = web_llm_tracing_policy_error("llm", {"tracing": tracing})

        assert error == LLM_TRACING_POLICY_ERROR
        assert "credential-canary" not in error


def test_core_config_has_no_web_runtime_import() -> None:
    repo_root = Path(__file__).parents[3]
    tree = ast.parse((repo_root / "src/elspeth/core/config.py").read_text(encoding="utf-8"))

    imported_modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported_modules.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None)

    assert not any(module == "elspeth.web" or module.startswith("elspeth.web.") for module in imported_modules)
