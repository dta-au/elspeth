"""A fixed input schema must declare the fields the author configured reads of.

An input column the author explicitly configured the transform to read,
omitted from a ``mode: fixed`` schema, is incoherent: the input model is
``extra='forbid'`` over the declared fields, so any row carrying the column
is rejected before the transform runs — the configured read can never
succeed (elspeth-d3958d90f5). The batch family reaches the same state by
injecting the column into ``required_fields`` on a directly-constructed
SchemaConfig — bypassing the ``from_dict`` validator — but the incoherence
predates the injection: the authored option itself names the column.

Only AUTHORED options participate. Option defaults are exempt —
``batch_replicate.copies_field`` defaults to ``"copies"`` with documented
row-absence fallback semantics, so a fixed schema without that column is a
legitimate "always default_copies" config, not an error.

The guard lives in ``BaseTransform._initialize_declared_input_fields`` (the
one seam every registered transform crosses immediately after config
validation) and fires only for ``mode: fixed``: flexible schemas admit the
column as an extra, and observed schemas declare nothing to contradict.
"""

from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.azure.document_intelligence import AzureDocumentIntelligence
from elspeth.plugins.transforms.batch_outlier_annotator import BatchOutlierAnnotator
from elspeth.plugins.transforms.keyword_filter import KeywordFilter

_DI_BASE = {
    "endpoint": "https://test.cognitiveservices.azure.com",
    "api_key": "k",
    "model_id": "prebuilt-layout",
    "source_mode": "url",
    "source_field": "doc_url",
    "content_field": "di_content",
}


class TestFixedSchemaOmittingConsumedFieldIsRejected:
    def test_batch_outlier_annotator_value_field_must_be_declared(self) -> None:
        """The ticket's repro: value_field absent from fixed fields."""
        with pytest.raises(PluginConfigError, match=r"'score' \(named by value_field\)"):
            BatchOutlierAnnotator(
                {
                    "value_field": "score",
                    "schema": {"mode": "fixed", "fields": ["a: str", "b: str"]},
                }
            )

    def test_azure_document_intelligence_source_field_must_be_declared(self) -> None:
        """No required_fields injection involved — the config option alone."""
        with pytest.raises(PluginConfigError, match="doc_url"):
            AzureDocumentIntelligence(
                {
                    **_DI_BASE,
                    "schema": {"mode": "fixed", "fields": ["doc_id: str"]},
                }
            )

    def test_keyword_filter_scan_fields_must_be_declared(self) -> None:
        """Plural column options (fields: [...]) are covered too."""
        with pytest.raises(PluginConfigError, match="text"):
            KeywordFilter(
                {
                    "fields": "text",
                    "blocked_patterns": ["x"],
                    "schema": {"mode": "fixed", "fields": ["other: str"]},
                }
            )


class TestSatisfiableShapesStillConstruct:
    def test_fixed_schema_declaring_the_consumed_field_constructs(self) -> None:
        t = BatchOutlierAnnotator(
            {
                "value_field": "score",
                "schema": {"mode": "fixed", "fields": ["a: str", "score: float"]},
            }
        )
        assert "score" in t.input_schema.model_fields

    def test_flexible_schema_omitting_the_consumed_field_constructs(self) -> None:
        """Flexible mode admits the column as an extra — not fatal."""
        t = BatchOutlierAnnotator(
            {
                "value_field": "score",
                "schema": {"mode": "flexible", "fields": ["a: str"]},
            }
        )
        assert t.input_schema.model_config.get("extra") == "allow"

    def test_observed_schema_constructs(self) -> None:
        t = BatchOutlierAnnotator(
            {
                "value_field": "score",
                "schema": {"mode": "observed"},
            }
        )
        assert t is not None

    def test_azure_di_fixed_schema_declaring_source_field_constructs(self) -> None:
        t = AzureDocumentIntelligence(
            {
                **_DI_BASE,
                "schema": {"mode": "fixed", "fields": ["doc_id: str", "doc_url: str"]},
            }
        )
        assert "doc_url" in t.input_schema.model_fields

    def test_defaulted_column_option_does_not_force_declaration(self) -> None:
        """An option the author never wrote expresses no read intent.

        batch_replicate's ``copies_field`` defaults to ``"copies"`` and a row
        without it uses ``default_copies`` — a fixed schema omitting the
        default column is a legitimate always-default config.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        t = BatchReplicate(
            {
                "schema": {"mode": "fixed", "fields": ["id: int"]},
                "include_copy_index": True,
            }
        )
        assert "copies" not in t.input_schema.model_fields

    def test_authored_column_option_matching_the_default_still_fires(self) -> None:
        """Explicitly writing the option is read intent, even at the default value."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        with pytest.raises(PluginConfigError, match="copies_field"):
            BatchReplicate(
                {
                    "copies_field": "copies",
                    "schema": {"mode": "fixed", "fields": ["id: int"]},
                }
            )
