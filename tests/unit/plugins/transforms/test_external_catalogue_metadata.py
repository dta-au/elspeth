"""Reference-content contract for external-call and provider transforms."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts import Determinism
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.token_usage import TokenUsage
from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
)
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafetyConfig
from elspeth.plugins.transforms.rag.config import PROVIDERS
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

EXPECTED_EXTERNAL_TAGS = {
    "aws_bedrock_content_safety": ("aws", "bedrock", "content-safety"),
    "aws_bedrock_prompt_shield": ("aws", "bedrock", "prompt-shield"),
    "aws_textract_document_analysis": ("aws", "textract", "document", "ocr", "enrichment"),
    "aws_textract_inline_analysis": ("aws", "textract", "ocr", "inline", "blob", "enrichment"),
    "azure_content_safety": ("azure", "content-safety", "moderation"),
    "azure_document_intelligence": ("azure", "document", "ocr", "enrichment", "http"),
    "azure_prompt_shield": ("azure", "prompt-shield", "security"),
    "blob_fetch": ("http", "network", "blob"),
    "llm": ("llm", "generation", "structured-output"),
    "rag_retrieval": ("rag", "retrieval", "vector-search"),
    "web_scrape": ("http", "network", "scraping"),
}
EXPECTED_EXTERNAL_NAMES = set(EXPECTED_EXTERNAL_TAGS)
EXTERNAL_REFERENCES = tuple(
    reference
    for reference in discover_builtin_references()
    if reference.kind == "transform" and reference.plugin_cls.determinism in {Determinism.EXTERNAL_CALL, Determinism.NON_DETERMINISTIC}
)
EXTERNAL_BY_NAME = {reference.plugin_cls.name: reference for reference in EXTERNAL_REFERENCES}

_REFERENCE_FIELDS = ("usage_when_to_use", "usage_when_not_to_use", "example_use", "capability_tags")
_PLACEHOLDER_MARKERS = ("todo", "tbd", "replace-me", "placeholder", "see the technical description")
_PROFILED_NAMES = {"llm", "aws_bedrock_prompt_shield", "aws_bedrock_content_safety"}
_REMOTE_CONTENT_PRODUCERS = {
    "web_scrape",
    "blob_fetch",
    "llm",
    "rag_retrieval",
    "aws_textract_document_analysis",
    "aws_textract_inline_analysis",
    "azure_document_intelligence",
}

_REQUIRED_GUIDANCE = {
    "web_scrape": (
        ("public http(s)", "audited", "markdown", "plain text", "untrusted before llm"),
        ("authenticated apis", "binary documents"),
    ),
    "blob_fetch": (
        ("authorized http(s)", "original", "mime", "size", "sha-256", "untrusted before llm"),
        ("semantic extraction", "origin-auth"),
    ),
    "llm": (
        ("operator-approved", "profile", "prompts", "responses", "model", "tokens", "untrusted before llm"),
        ("provider credentials", "endpoints", "web-authored options"),
    ),
    "rag_retrieval": (
        ("existing chroma", "azure search", "ranked", "provenance", "untrusted before llm"),
        ("indexing", "answer generation"),
    ),
    "aws_bedrock_prompt_shield": (
        ("pre-llm", "prompt attack", "operator profile", "default aws credential chain"),
        ("post-llm", "harmful content"),
    ),
    "aws_bedrock_content_safety": (
        ("post-llm", "harmful content", "source: output", "output-control credit", "operator profile"),
        ("prompt attack", "source: input"),
    ),
    "aws_textract_document_analysis": (
        ("asynchronous", "s3", "ocr", "forms", "tables", "s3 read", "untrusted before llm"),
        ("inline bytes", "synchronous"),
    ),
    "aws_textract_inline_analysis": (
        ("synchronous", "blob_rows", "payload-store", "ocr", "single-page", "5 mib", "untrusted before llm"),
        ("multipage", "s3", "billable"),
    ),
    "azure_content_safety": (
        ("hate", "violence", "sexual", "self-harm", "threshold"),
        ("threshold 6", "effectively non-blocking"),
    ),
    "azure_prompt_shield": (
        ("pre-llm", "jailbreak", "prompt injection", "user_prompt", "document", "both"),
        ("harmful-content moderation",),
    ),
    "azure_document_intelligence": (
        ("url", "base64", "request audit", "encoded body", "untrusted before llm"),
        ("credential", "data-retention"),
    ),
}


def _declaring_node(reference: BuiltinReference) -> Mapping[str, Any]:
    example = reference.plugin_cls.example_use
    assert isinstance(example, str)
    parsed = load_bounded_pipeline_yaml(example)
    assert set(parsed) == {"transform"}
    node = cast(Mapping[str, Any], parsed["transform"])
    assert node["plugin"] == reference.plugin_cls.name
    assert set(node) <= {"plugin", "options"}
    return node


def _options(reference: BuiltinReference) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _declaring_node(reference)["options"])


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
            "secret_key": "catalogue-reference-test-secret-key-at-least-32",
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
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


def _lower_profiled_options(reference: BuiltinReference) -> dict[str, Any]:
    authored = dict(_options(reference))
    alias = cast(str, authored.pop("profile"))
    lowered = _operator_profile_registry().lower_options(
        PluginId("transform", reference.plugin_cls.name),
        alias=alias,
        safe_options=authored,
    )
    assert deep_thaw(lowered.audit_safe_options) == dict(_options(reference))
    executable = _replace_secret_refs(deep_thaw(lowered.executable_options))
    assert isinstance(executable, dict)
    return executable


def _runtime_options(reference: BuiltinReference) -> dict[str, Any]:
    if reference.plugin_cls.name in _PROFILED_NAMES:
        return _lower_profiled_options(reference)
    options = _replace_secret_refs(_options(reference))
    assert isinstance(options, dict)
    return options


def _composer_hints(name: str) -> str:
    assistance = EXTERNAL_BY_NAME[name].plugin_cls.get_agent_assistance(issue_code=None)
    assert assistance is not None
    return " ".join(assistance.composer_hints).casefold()


def _assert_example_credentials_are_safe(reference: BuiltinReference) -> None:
    options = _options(reference)
    additional_fields = allowed_secret_ref_fields("transform", reference.plugin_cls.name)
    assert not collect_credential_field_violations(options, additional_credential_fields=additional_fields)
    assert not collect_disallowed_secret_ref_markers(options, additional_allowed_fields=additional_fields)


def _assert_constructor_is_side_effect_free(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    options = _runtime_options(reference)
    config_model = reference.plugin_cls.get_config_model(options)
    assert config_model is not None
    config_model.from_dict(options, plugin_name=reference.plugin_cls.name)
    with plugin_preflight_mode(True):
        transform = reference.plugin_cls(options)
    try:
        assert tuple(tmp_path.rglob("*")) == before
    finally:
        transform.close()
    assert tuple(tmp_path.rglob("*")) == before


def test_external_catalogue_discovers_every_and_only_external_or_nondeterministic_transform() -> None:
    assert set(EXTERNAL_BY_NAME) == EXPECTED_EXTERNAL_NAMES


@pytest.mark.parametrize("reference", EXTERNAL_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_external_catalogue_reference_content_is_class_owned_specific_valid_and_truthful(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_cls = reference.plugin_cls
    assert all(field_name in plugin_cls.__dict__ for field_name in _REFERENCE_FIELDS)
    assert_reference_text(plugin_cls)
    assert_reference_tags(plugin_cls)
    assert plugin_cls.capability_tags == EXPECTED_EXTERNAL_TAGS[plugin_cls.name]
    _declaring_node(reference)
    _assert_example_credentials_are_safe(reference)
    if plugin_cls.name not in _PROFILED_NAMES:
        parse_and_validate_example(reference)

    to_use = cast(str, plugin_cls.usage_when_to_use).casefold()
    not_to_use = cast(str, plugin_cls.usage_when_not_to_use).casefold()
    required_use, required_avoid = _REQUIRED_GUIDANCE[plugin_cls.name]
    assert all(term in to_use for term in required_use)
    assert all(term in not_to_use for term in required_avoid)

    all_reference_text = " ".join(cast(str, getattr(plugin_cls, name)) for name in _REFERENCE_FIELDS[:-1]).casefold()
    assert not any(marker in all_reference_text for marker in _PLACEHOLDER_MARKERS)
    _assert_constructor_is_side_effect_free(reference, tmp_path, monkeypatch)


def test_http_fetch_examples_use_wire_visible_non_secret_identity_and_reason_text() -> None:
    web_http = cast(Mapping[str, Any], _options(EXTERNAL_BY_NAME["web_scrape"])["http"])
    blob_http = cast(Mapping[str, Any], _options(EXTERNAL_BY_NAME["blob_fetch"])["http"])

    assert web_http["abuse_contact"] == "catalogue-ops@example.org"
    assert web_http["scraping_reason"] == "Audited public policy monitoring"
    assert blob_http["abuse_contact"] == "catalogue-ops@example.org"
    assert blob_http["fetch_reason"] == "Preserve approved public reference files"
    assert all(not isinstance(value, Mapping) for value in (*web_http.values(), *blob_http.values()))


@pytest.mark.parametrize(
    ("name", "profile", "expected_keys"),
    [
        (
            "llm",
            "approved-structured-generation",
            {"profile", "prompt_template", "required_input_fields", "response_field", "schema"},
        ),
        (
            "aws_bedrock_prompt_shield",
            "approved-input-guardrail",
            {"profile", "fields", "schema"},
        ),
        (
            "aws_bedrock_content_safety",
            "approved-output-guardrail",
            {"profile", "fields", "source", "schema"},
        ),
    ],
)
def test_operator_profiled_examples_expose_only_an_opaque_profile_and_safe_row_options(
    name: str,
    profile: str,
    expected_keys: set[str],
) -> None:
    options = _options(EXTERNAL_BY_NAME[name])

    assert options["profile"] == profile
    assert set(options) == expected_keys
    assert (
        not {
            "provider",
            "model",
            "endpoint",
            "endpoint_url",
            "api_key",
            "access_key",
            "secret_key",
            "session_token",
            "guardrail_identifier",
            "guardrail_version",
            "region",
        }
        & options.keys()
    )

    plugin_id = PluginId("transform", name)
    full_schema = create_catalog_service().get_schema("transform", name)
    public_schema = (
        _operator_profile_registry()
        .public_schema(
            plugin_id,
            full_schema,
            available_aliases=(profile,),
        )
        .json_schema
    )
    assert list(Draft202012Validator(public_schema).iter_errors(dict(options))) == []

    executable = _lower_profiled_options(EXTERNAL_BY_NAME[name])
    if name == "llm":
        assert executable["provider"] == "openrouter"
        assert executable["model"] == "openai/gpt-4o"
        assert executable["api_key"] == "catalogue-reference-secret"
    else:
        assert executable["guardrail_version"] == "1"
        assert executable["region"] == "ap-southeast-2"
        assert "guardrail_identifier" in executable


def test_bedrock_content_safety_example_is_an_effective_output_control() -> None:
    assert _options(EXTERNAL_BY_NAME["aws_bedrock_content_safety"])["source"] == "OUTPUT"


def test_textract_guidance_does_not_recommend_an_unregistered_synchronous_plugin() -> None:
    avoid = cast(str, EXTERNAL_BY_NAME["aws_textract_document_analysis"].plugin_cls.usage_when_not_to_use).casefold()
    assert "use the separate synchronous textract plugin" not in avoid
    assert "when that surface is available" not in avoid


def test_azure_document_intelligence_example_uses_a_supported_secret_ref_marker() -> None:
    reference = EXTERNAL_BY_NAME["azure_document_intelligence"]
    options = _options(reference)
    example = cast(str, reference.plugin_cls.example_use)

    assert options["api_key"] == {"secret_ref": "AZURE_DOCUMENT_INTELLIGENCE_KEY"}
    assert "${" not in example


def test_rag_azure_example_keeps_auth_and_index_settings_inside_provider_config() -> None:
    options = _options(EXTERNAL_BY_NAME["rag_retrieval"])
    provider_config = cast(Mapping[str, Any], options["provider_config"])

    assert options["provider"] == "azure_search"
    assert provider_config == {
        "endpoint": "https://catalogue-reference.search.windows.net",
        "index": "approved-documents",
        "api_key": {"secret_ref": "AZURE_SEARCH_API_KEY"},
        "search_mode": "hybrid",
    }
    assert not {"endpoint", "index", "api_key", "search_mode"} & (options.keys() - {"provider_config"})


@pytest.mark.parametrize("name", sorted(_REMOTE_CONTENT_PRODUCERS))
def test_remote_content_producers_warn_that_content_is_untrusted_before_llm_use(name: str) -> None:
    plugin_cls = EXTERNAL_BY_NAME[name].plugin_cls
    guidance = f"{plugin_cls.usage_when_to_use} {plugin_cls.usage_when_not_to_use}".casefold()
    assert "untrusted before llm" in guidance


def test_azure_content_safety_example_configures_all_category_thresholds_below_the_noop_value() -> None:
    thresholds = cast(Mapping[str, Any], _options(EXTERNAL_BY_NAME["azure_content_safety"])["thresholds"])
    assert thresholds == {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2}


def test_azure_prompt_shield_example_selects_document_analysis_for_retrieved_context() -> None:
    options = _options(EXTERNAL_BY_NAME["azure_prompt_shield"])
    assert options["fields"] == ["retrieved_context"]
    assert options["analysis_type"] == "document"


def test_document_intelligence_key_value_example_enables_the_required_analyze_feature() -> None:
    reference = EXTERNAL_BY_NAME["azure_document_intelligence"]
    options = _runtime_options(reference)

    assert options["features"] == ["keyValuePairs"]
    with plugin_preflight_mode(True):
        transform = reference.plugin_cls(options)
    try:
        assert "features=keyValuePairs" in transform._analyze_url()
    finally:
        transform.close()


def test_prompt_shield_both_documents_two_analyses_but_one_audited_http_call() -> None:
    class _Response:
        text = '{"userPromptAnalysis":{"attackDetected":false},"documentsAnalysis":[{"attackDetected":false}]}'

        def raise_for_status(self) -> None:
            return None

    class _RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Mapping[str, object]]] = []

        def post(self, url: str, *, json: Mapping[str, object]) -> _Response:
            self.calls.append((url, json))
            return _Response()

        def close(self) -> None:
            return None

    reference = EXTERNAL_BY_NAME["azure_prompt_shield"]
    options = _runtime_options(reference)
    options["analysis_type"] = "both"
    with plugin_preflight_mode(True):
        transform = reference.plugin_cls(options)
    client = _RecordingClient()
    transform._http_clients["catalogue-state"] = client
    try:
        assert transform._analyze_prompt("retrieved text", "catalogue-state") == {
            "user_prompt_attack": False,
            "document_attack": False,
        }
        assert len(client.calls) == 1
        assert client.calls[0][1] == {
            "userPrompt": "retrieved text",
            "documents": ["retrieved text"],
        }

        guidance = (f"{reference.plugin_cls.usage_when_not_to_use} {_composer_hints('azure_prompt_shield')}").casefold()
        assert "two analyses" in guidance
        assert "one audited http call" in guidance
        assert "two calls" not in guidance
    finally:
        transform.close()


def test_llm_token_guidance_preserves_provider_omission_as_unknown() -> None:
    guidance = cast(str, EXTERNAL_BY_NAME["llm"].plugin_cls.usage_when_to_use).casefold()
    unknown = TokenUsage.unknown()

    assert "tokens when reported by the provider" in guidance
    assert unknown.is_known is False
    assert unknown.has_data is False
    assert unknown.to_dict() == {}


def test_content_safety_threshold_description_matches_strict_greater_than_runtime_semantics() -> None:
    schema = AzureContentSafetyConfig.model_json_schema()
    thresholds_schema = cast(Mapping[str, object], cast(Mapping[str, object], schema["$defs"])["ContentSafetyThresholds"])
    description = cast(str, thresholds_schema["description"]).casefold()

    assert "severity > threshold" in description
    assert "threshold of 0 allows severity 0" in description
    assert "threshold of 6 blocks nothing" in description


def test_content_safety_composer_hint_preserves_remote_content_trust_tier() -> None:
    hints = _composer_hints("azure_content_safety")

    assert "tier 3" not in hints
    assert "tier 2" not in hints
    assert "remote content remains untrusted" in hints
    assert "prompt-injection defenses" in hints


def test_rag_composer_hints_name_only_real_authored_and_provider_config_fields() -> None:
    hints = _composer_hints("rag_retrieval")
    provider_fields = {name: set(config_cls.model_fields) for name, (config_cls, _factory) in PROVIDERS.items()}

    assert "collection" in provider_fields["chroma"]
    assert "index" in provider_fields["azure_search"]
    assert "provider_config.collection" in hints
    assert "provider_config.index" in hints
    assert "min_score" in hints
    assert "on_no_results" in hints
    assert "collection_name" not in hints
    assert "score_threshold" not in hints
    assert "on_zero_results" not in hints


def test_blob_fetch_composer_hint_recommends_only_a_registered_blob_parser() -> None:
    hints = _composer_hints("blob_fetch")
    registered = {plugin_cls.name for plugin_cls in get_shared_plugin_manager().get_transforms()}

    assert "blob_csv_expand" in registered
    assert "blob_csv_expand" in hints
    assert "blob_json_expand" not in registered
    assert "blob_json_expand" not in hints
