"""Bedrock guardrail row deadline is operator-configurable (maintainer ruling on AWS review #4)."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws import _guardrail_transform
from elspeth.plugins.transforms.aws._guardrail_transform import BedrockGuardrailTransformConfig
from elspeth.plugins.transforms.aws.bedrock_content_safety import AWSBedrockContentSafety
from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield


def _config(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "guardrail_identifier": "privateguardrail",
        "guardrail_version": "7",
        "region": "us-east-1",
        "fields": ["prompt"],
        "schema": {"mode": "observed"},
    }
    values.update(overrides)
    return values


def test_config_declares_row_deadline_seconds_defaulting_to_300() -> None:
    assert "row_deadline_seconds" in BedrockGuardrailTransformConfig.model_fields
    assert BedrockGuardrailTransformConfig.model_fields["row_deadline_seconds"].default == 300
    assert _guardrail_transform.GUARDRAIL_ROW_DEADLINE_SECONDS == 300


@pytest.mark.parametrize("transform_cls", [AWSBedrockPromptShield, AWSBedrockContentSafety])
def test_default_deadline_is_300_seconds(transform_cls: type[Any]) -> None:
    transform = transform_cls(_config())
    assert transform._row_deadline_seconds == 300


@pytest.mark.parametrize("transform_cls", [AWSBedrockPromptShield, AWSBedrockContentSafety])
def test_configured_deadline_reaches_the_transform(transform_cls: type[Any]) -> None:
    transform = transform_cls(_config(row_deadline_seconds=45))
    assert transform._row_deadline_seconds == 45


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_deadline_is_rejected(bad: int) -> None:
    with pytest.raises(PluginConfigError):
        AWSBedrockPromptShield(_config(row_deadline_seconds=bad))
