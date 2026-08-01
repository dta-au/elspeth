"""Bedrock Guardrail harmful-content shield."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from elspeth.contracts import Determinism
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority
from elspeth.plugins.transforms.aws._guardrail_transform import BedrockGuardrailTransformBase, BedrockGuardrailTransformConfig
from elspeth.plugins.transforms.aws.guardrails_client import HARMFUL_CONTENT_FILTERS, GuardrailSource


class AWSBedrockContentSafetyConfig(BedrockGuardrailTransformConfig):
    """Explicit CLI configuration for harmful-content checks."""

    source: Literal["INPUT", "OUTPUT"] = Field(
        default="OUTPUT",
        description="Bedrock Guardrail assessment direction for the selected fields.",
    )


class AWSBedrockContentSafety(BedrockGuardrailTransformBase):
    """Block configured harmful-content categories through Bedrock Guardrails."""

    name = "aws_bedrock_content_safety"
    determinism = Determinism.EXTERNAL_CALL
    web_config_authority = WebConfigAuthority.OPERATOR_PROFILED
    policy_capabilities = frozenset(
        {
            CapabilityDeclaration(
                PluginCapability.CONTENT_SAFETY,
                ControlRole.OUTPUT,
                blocks_positive_detection=True,
            )
        }
    )
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:a2616df0b0a79c6b"
    config_model = AWSBedrockContentSafetyConfig
    _required_filters = HARMFUL_CONTENT_FILTERS
    _detected_reason = "content_safety_violation"
    _probe_field = "bedrock_content_safety_probe_text"
    capability_tags: tuple[str, ...] = ("aws", "bedrock", "content-safety")

    usage_when_to_use = (
        "Use as a post-LLM harmful content control through an opaque operator profile. Set "
        "source: OUTPUT; that direction is required for output-control credit."
    )
    usage_when_not_to_use = (
        "Not for pre-LLM prompt attack screening, and source: INPUT does not satisfy the post-generation "
        "output-control role. Use aws_bedrock_prompt_shield before the LLM instead."
    )
    example_use = (
        "transform:\n"
        "  plugin: aws_bedrock_content_safety\n"
        "  options:\n"
        "    profile: approved-output-guardrail\n"
        "    fields: [generated_text]\n"
        "    source: OUTPUT\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def is_effective_blocking_control(
        cls,
        *,
        capability: PluginCapability,
        role: ControlRole,
        options: Mapping[str, object],
    ) -> bool:
        if not super().is_effective_blocking_control(capability=capability, role=role, options=options):
            return False
        source: object = "OUTPUT"
        if "source" in options:
            source = options["source"]
        return type(source) is str and source == "OUTPUT"

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = AWSBedrockContentSafetyConfig.from_dict(config, plugin_name=self.name)
        super().__init__(config, cfg, "AWSBedrockContentSafety")
        self._source: GuardrailSource = cfg.source

    @property
    def _guardrail_source(self) -> GuardrailSource:
        return self._source

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Block harmful text using an operator-approved Bedrock Guardrail profile.",
                composer_hints=(
                    "Use OUTPUT after an LLM or INPUT when screening inbound content.",
                    "AWS categories do not include Azure Content Safety self_harm parity.",
                    "Both detect-only positives and Guardrail interventions are blocked.",
                ),
            )
        return None
