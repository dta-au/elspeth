"""Registry-wide acceptance gate for built-in catalogue reference content."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from functools import cache
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
)
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    discover_builtin_references,
    parse_and_validate_example,
)

EXPECTED_BUILTIN_IDENTITIES = frozenset(
    {
        "source:aws_s3",
        "source:azure_blob",
        "source:csv",
        "source:dataverse",
        "source:json",
        "source:null",
        "source:text",
        "transform:aws_bedrock_content_safety",
        "transform:aws_bedrock_prompt_shield",
        "transform:aws_textract_document_analysis",
        "transform:azure_content_safety",
        "transform:azure_document_intelligence",
        "transform:azure_prompt_shield",
        "transform:batch_classifier_metrics",
        "transform:batch_data_quality_report",
        "transform:batch_distribution_profile",
        "transform:batch_drift_compare",
        "transform:batch_effect_size",
        "transform:batch_experiment_compare",
        "transform:batch_outlier_annotator",
        "transform:batch_paired_preference",
        "transform:batch_replicate",
        "transform:batch_stats",
        "transform:batch_threshold_summary",
        "transform:batch_top_k",
        "transform:blob_csv_expand",
        "transform:blob_fetch",
        "transform:field_mapper",
        "transform:json_explode",
        "transform:keyword_filter",
        "transform:line_explode",
        "transform:llm",
        "transform:passthrough",
        "transform:rag_retrieval",
        "transform:report_assemble",
        "transform:truncate",
        "transform:type_coerce",
        "transform:value_transform",
        "transform:web_scrape",
        "sink:aws_s3",
        "sink:azure_blob",
        "sink:chroma_sink",
        "sink:csv",
        "sink:database",
        "sink:dataverse",
        "sink:json",
        "sink:text",
    }
)

# ``source:null`` is documented for resume graph reconstruction but is hidden
# from ordinary web-authoring selection. It remains subject to every per-class
# content assertion above; only the user-visible cross-plugin uniqueness check
# excludes it.
DOCUMENTED_HIDDEN_IDENTITIES = frozenset({"source:null"})
OPERATOR_PROFILED_IDENTITIES = frozenset(
    {
        "transform:llm",
        "transform:aws_bedrock_prompt_shield",
        "transform:aws_bedrock_content_safety",
    }
)
REFERENCE_FIELDS = (
    "usage_when_to_use",
    "usage_when_not_to_use",
    "example_use",
    "capability_tags",
)
REFERENCES = discover_builtin_references()
REFERENCES_BY_IDENTITY = {f"{reference.kind}:{reference.plugin_cls.name}": reference for reference in REFERENCES}


def _identity(reference: BuiltinReference) -> str:
    return f"{reference.kind}:{reference.plugin_cls.name}"


def _normalize_reference_text(value: str) -> str:
    """Apply a fixed exact-comparison normalization, independent of production."""
    return " ".join(value.split()).casefold().rstrip(".!")


def _profiled_authored_options(reference: BuiltinReference) -> dict[str, Any]:
    parsed = load_bounded_pipeline_yaml(reference.plugin_cls.example_use)
    assert set(parsed) == {"transform"}
    node = cast(Mapping[str, Any], parsed["transform"])
    assert node["plugin"] == reference.plugin_cls.name
    assert set(node) <= {"plugin", "options"}
    options = cast(Mapping[str, Any], node["options"])

    credential_fields = allowed_secret_ref_fields(reference.kind, reference.plugin_cls.name)
    assert not collect_credential_field_violations(options, additional_credential_fields=credential_fields)
    assert not collect_disallowed_secret_ref_markers(options, additional_allowed_fields=credential_fields)
    return dict(options)


def _replace_secret_refs(value: object) -> object:
    if isinstance(value, Mapping):
        if "secret_ref" in value and set(value) <= {"secret_ref", "secret_scope"}:
            return "catalogue-reference-secret"
        return {key: _replace_secret_refs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_secret_refs(child) for child in value]
    return value


@cache
def _operator_profile_registry() -> OperatorProfileRegistry:
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "secret_key": "catalogue-reference-test-secret-key-at-least-32",  # secret-scan: allow-this-line
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",  # secret-scan: allow-this-line
            "plugin_allowlist": (
                "transform:aws_bedrock_prompt_shield",
                "transform:aws_bedrock_content_safety",
            ),
            "llm_profiles": {
                "approved-structured-generation": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4o",
                    "credential_scope": "server",
                    "credential_ref": "OPENROUTER_API_KEY",
                }
            },
            "default_llm_profile": "approved-structured-generation",
            "bedrock_guardrail_profiles": (
                {
                    "alias": "approved-input-guardrail",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "catalogueinputguardrail",
                    "guardrail_version": "1",
                    "region": "ap-southeast-2",
                },
                {
                    "alias": "approved-output-guardrail",
                    "plugin": "aws_bedrock_content_safety",
                    "guardrail_identifier": "catalogueoutputguardrail",
                    "guardrail_version": "1",
                    "region": "ap-southeast-2",
                },
            ),
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    return OperatorProfileRegistry(policy=policy, settings=runtime)


def test_registry_contains_the_exact_accepted_builtin_inventory() -> None:
    assert len(REFERENCES) == 47
    assert Counter(reference.kind for reference in REFERENCES) == {
        "source": 7,
        "transform": 32,
        "sink": 8,
    }
    assert {_identity(reference) for reference in REFERENCES} == EXPECTED_BUILTIN_IDENTITIES


@pytest.mark.parametrize("reference", REFERENCES, ids=_identity)
def test_every_builtin_owns_specific_reference_content(reference: BuiltinReference) -> None:
    assert all(field_name in reference.plugin_cls.__dict__ for field_name in REFERENCE_FIELDS)
    assert_reference_text(reference.plugin_cls)
    assert_reference_tags(reference.plugin_cls)


DIRECT_CONFIG_REFERENCES = tuple(reference for reference in REFERENCES if _identity(reference) not in OPERATOR_PROFILED_IDENTITIES)


def test_operator_profiled_exception_set_is_fixed_and_exhaustive() -> None:
    profiled_examples: set[str] = set()
    for reference in REFERENCES:
        if reference.kind != "transform":
            continue
        parsed = load_bounded_pipeline_yaml(reference.plugin_cls.example_use)
        assert set(parsed) == {"aggregations" if reference.plugin_cls.is_batch_aware else "transform"}
        if reference.plugin_cls.is_batch_aware:
            continue
        node = cast(Mapping[str, Any], parsed["transform"])
        options = cast(Mapping[str, Any], node.get("options", {}))
        if "profile" in options:
            profiled_examples.add(_identity(reference))

    assert profiled_examples == OPERATOR_PROFILED_IDENTITIES
    assert len(DIRECT_CONFIG_REFERENCES) == 44


@pytest.mark.parametrize("reference", DIRECT_CONFIG_REFERENCES, ids=_identity)
def test_direct_config_builtin_examples_are_valid(reference: BuiltinReference) -> None:
    parse_and_validate_example(reference)


@pytest.mark.parametrize(
    "identity",
    sorted(OPERATOR_PROFILED_IDENTITIES),
)
def test_operator_profiled_builtin_examples_are_valid(identity: str) -> None:
    reference = REFERENCES_BY_IDENTITY[identity]
    authored_options = _profiled_authored_options(reference)
    alias = cast(str, authored_options["profile"])
    plugin_id = PluginId(reference.kind, reference.plugin_cls.name)
    registry = _operator_profile_registry()

    full_schema = create_catalog_service().get_schema(reference.kind, reference.plugin_cls.name)
    public_schema = registry.public_schema(
        plugin_id,
        full_schema,
        available_aliases=(alias,),
    ).json_schema
    assert list(Draft202012Validator(public_schema).iter_errors(authored_options)) == []

    safe_options = dict(authored_options)
    safe_options.pop("profile")
    lowered = registry.lower_options(plugin_id, alias=alias, safe_options=safe_options)
    assert deep_thaw(lowered.audit_safe_options) == authored_options
    executable_options = _replace_secret_refs(deep_thaw(lowered.executable_options))
    assert isinstance(executable_options, dict)
    config_model = reference.plugin_cls.get_config_model(executable_options)
    assert config_model is not None
    config_model.from_dict(executable_options, plugin_name=reference.plugin_cls.name)


@pytest.mark.parametrize(
    "field_name",
    ("usage_when_to_use", "usage_when_not_to_use", "example_use"),
)
def test_user_visible_reference_content_is_exactly_unique(field_name: str) -> None:
    values: dict[str, list[str]] = defaultdict(list)
    for reference in REFERENCES:
        identity = _identity(reference)
        if identity in DOCUMENTED_HIDDEN_IDENTITIES:
            continue
        value = getattr(reference.plugin_cls, field_name)
        assert isinstance(value, str)
        values[_normalize_reference_text(value)].append(identity)

    duplicates = {value: identities for value, identities in values.items() if len(identities) > 1}
    assert not duplicates, f"duplicate normalized {field_name} values: {duplicates!r}"
