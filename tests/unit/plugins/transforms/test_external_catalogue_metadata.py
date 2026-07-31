"""Reference-content contract for external-call and provider transforms."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts import Determinism
from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
)
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
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
        if set(value) == {"secret_ref"}:
            return "catalogue-reference-secret"
        return {key: _replace_secret_refs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_secret_refs(child) for child in value]
    return value


def _runtime_options(reference: BuiltinReference) -> dict[str, Any]:
    options = _replace_secret_refs(_options(reference))
    assert isinstance(options, dict)
    if reference.plugin_cls.name == "llm":
        profile = options.pop("profile")
        assert profile == "approved-structured-generation"
        return {
            "provider": "openrouter",
            "model": "openai/gpt-4o",
            "api_key": "catalogue-reference-secret",
            **options,
        }
    if reference.plugin_cls.name in {"aws_bedrock_prompt_shield", "aws_bedrock_content_safety"}:
        profile = options.pop("profile")
        assert profile in {"approved-input-guardrail", "approved-output-guardrail"}
        return {
            "guardrail_identifier": "catalogueguardrail",
            "guardrail_version": "1",
            "region": "ap-southeast-2",
            **options,
        }
    return options


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
