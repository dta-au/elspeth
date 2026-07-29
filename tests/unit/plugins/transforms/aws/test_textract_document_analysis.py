"""Unit tests for the Amazon Textract document-analysis transform."""

from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws.textract_document_analysis import (
    AWSTextractDocumentAnalysisConfig,
)


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "region": "ap-southeast-2",
        "bucket_field": "document_bucket",
        "key_field": "document_key",
        "feature_types": ["FORMS", "TABLES"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


def _load(**overrides: object) -> AWSTextractDocumentAnalysisConfig:
    return AWSTextractDocumentAnalysisConfig.from_dict(
        _config(**overrides),
        plugin_name="aws_textract_document_analysis",
    )


def test_minimal_default_chain_config() -> None:
    cfg = _load()

    assert cfg.auth_mode == "default_chain"
    assert cfg.feature_types == ["FORMS", "TABLES"]
    assert cfg.all_output_field_names() == ["textract_text"]


def test_secret_refs_mode_requires_access_and_secret_key() -> None:
    with pytest.raises(PluginConfigError, match="required together"):
        _load(auth_mode="secret_refs", aws_access_key_id="resolved-access-id")


def test_secret_refs_mode_accepts_pair_and_optional_session_token() -> None:
    cfg = _load(
        auth_mode="secret_refs",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        aws_session_token="resolved-session-token",
    )

    assert cfg.auth_mode == "secret_refs"
    assert cfg.aws_session_token == "resolved-session-token"


def test_default_chain_rejects_explicit_credentials() -> None:
    with pytest.raises(PluginConfigError, match="forbidden in default_chain"):
        _load(
            aws_access_key_id="resolved-access-id",
            aws_secret_access_key="resolved-secret-key",
        )


def test_session_token_without_credential_pair_fails() -> None:
    with pytest.raises(PluginConfigError, match="credential pair"):
        _load(auth_mode="secret_refs", aws_session_token="resolved-session-token")


def test_queries_require_queries_feature() -> None:
    with pytest.raises(PluginConfigError, match="QUERIES"):
        _load(queries=[{"text": "What is the total?"}])


def test_queries_feature_requires_queries() -> None:
    with pytest.raises(PluginConfigError, match="QUERIES"):
        _load(feature_types=["QUERIES"])


def test_duplicate_output_names_fail() -> None:
    with pytest.raises(PluginConfigError, match="Duplicate output field"):
        _load(text_field="result", metadata_field="result")


def test_at_least_one_output_is_required() -> None:
    with pytest.raises(PluginConfigError, match="At least one output target"):
        _load(text_field=None)


@pytest.mark.parametrize("feature", ["TABLE", "forms", "OCR", ""])
def test_unknown_feature_type_fails(feature: str) -> None:
    with pytest.raises(PluginConfigError, match="feature_types"):
        _load(feature_types=[feature])


def test_duplicate_feature_types_fail_without_reordering() -> None:
    with pytest.raises(PluginConfigError, match="duplicates"):
        _load(feature_types=["TABLES", "FORMS", "TABLES"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "ap_southeast_2"),
        ("bucket_field", "   "),
        ("key_field", "\t"),
        ("version_field", "\n"),
        ("text_field", ""),
        ("page_count_field", " "),
        ("metadata_field", "\t"),
        ("result_field", "\n"),
    ],
)
def test_invalid_region_or_field_name_fails(field: str, value: object) -> None:
    with pytest.raises(PluginConfigError):
        _load(**{field: value})


@pytest.mark.parametrize(
    "selector",
    ["0", "0-2", "2-1", "1-0", "*-2", "1--2", "abcdefghij"],
)
def test_invalid_query_page_selector_fails(selector: str) -> None:
    with pytest.raises(PluginConfigError, match="pages"):
        _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": [selector]}])


@pytest.mark.parametrize("pages", [["*", "2"], ["1", "1"], ["1-*", "1-*"]])
def test_ambiguous_or_duplicate_query_page_selectors_fail(pages: list[str]) -> None:
    with pytest.raises(PluginConfigError, match="pages"):
        _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": pages}])


@pytest.mark.parametrize("query_field", ["text", "alias"])
def test_query_text_and_alias_reject_provider_disallowed_characters(query_field: str) -> None:
    query: dict[str, object] = {"text": "Total?", query_field: "disallowed\u0000value"}
    with pytest.raises(PluginConfigError, match=query_field):
        _load(feature_types=["QUERIES"], queries=[query])


def test_more_than_thirty_queries_fails() -> None:
    queries = [{"text": f"Question {index}?"} for index in range(31)]
    with pytest.raises(PluginConfigError, match="queries"):
        _load(feature_types=["QUERIES"], queries=queries)


@pytest.mark.parametrize(
    "field",
    [
        "poll_interval_seconds",
        "poll_timeout_seconds",
        "batch_wait_timeout_seconds",
        "max_result_pages",
        "max_blocks",
        "max_result_bytes",
    ],
)
def test_positive_bounds_reject_zero(field: str) -> None:
    with pytest.raises(PluginConfigError, match=field):
        _load(**{field: 0})


def test_poll_backoff_multiplier_must_be_at_least_one() -> None:
    with pytest.raises(PluginConfigError, match="poll_backoff_multiplier"):
        _load(poll_backoff_multiplier=0.5)


def test_poll_max_interval_must_cover_initial_interval() -> None:
    with pytest.raises(PluginConfigError, match="poll_max_interval_seconds"):
        _load(poll_interval_seconds=2.0, poll_max_interval_seconds=1.0)


def test_full_configuration_declares_inputs_and_ordered_outputs() -> None:
    cfg = _load(
        auth_mode="secret_refs",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        version_field="document_version",
        feature_types=["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"],
        queries=[{"text": "What is the total?", "alias": "invoice_total", "pages": ["1-3", "5-*"]}],
        page_count_field="textract_page_count",
        metadata_field="textract_metadata",
        result_field="textract_native",
        extract={
            "pages": "textract_pages",
            "tables": "textract_tables",
            "forms": "textract_forms",
            "queries": "textract_queries",
            "signatures": "textract_signatures",
            "layout": "textract_layout",
        },
    )

    assert cfg.declared_input_fields == frozenset({"document_bucket", "document_key", "document_version"})
    assert cfg.configured_output_fields() == {
        "pages": "textract_pages",
        "tables": "textract_tables",
        "forms": "textract_forms",
        "queries": "textract_queries",
        "signatures": "textract_signatures",
        "layout": "textract_layout",
    }
    assert cfg.all_output_field_names() == [
        "textract_text",
        "textract_page_count",
        "textract_metadata",
        "textract_native",
        "textract_pages",
        "textract_tables",
        "textract_forms",
        "textract_queries",
        "textract_signatures",
        "textract_layout",
    ]
