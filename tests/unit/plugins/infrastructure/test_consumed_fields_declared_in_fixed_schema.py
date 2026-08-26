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

from typing import ClassVar

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


class TestProvenanceIsAttributedHonestly:
    """The message must name the option the author ACTUALLY wrote.

    ``declared_input_fields`` is multi-provenance: the author's own
    ``required_input_fields`` list, or — for the plugins whose config DERIVES it
    from an ordinary option — that option. The guard named
    ``required_input_fields`` for every member regardless, so an author who had
    never written that key was sent to edit it.

    ``blob_csv_expand`` is the worst case and the reason a generic label is not
    good enough on its own: ``blob_ref_field`` DEFAULTS to ``blob_ref``, so the
    message named a key the author never wrote, for a column they never
    mentioned, in a config containing neither string.

    A generic-but-true label beats a specific-but-false one. The base class
    cannot see which option a config property derived a name from, so where no
    column-naming option in the raw config accounts for the name it says so
    plainly rather than guessing — the rest of the message already names the
    offending FIELD, which is what an author searches their options for.
    """

    _HTTP: ClassVar[dict[str, str]] = {"abuse_contact": "abuse@example.com", "scraping_reason": "contract test"}

    def test_web_scrape_names_url_field_not_required_input_fields(self) -> None:
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        with pytest.raises(PluginConfigError) as excinfo:
            WebScrapeTransform(
                {
                    "url_field": "page_url",
                    "content_field": "c",
                    "fingerprint_field": "f",
                    "http": self._HTTP,
                    "schema": {"mode": "fixed", "fields": ["c: str", "f: str"]},
                }
            )

        assert "'page_url' (named by url_field)" in str(excinfo.value)
        assert "required_input_fields" not in str(excinfo.value).split("A 'mode: fixed'")[0]

    def test_blob_fetch_names_url_field(self) -> None:
        from elspeth.plugins.transforms.blob_fetch import BlobFetch

        with pytest.raises(PluginConfigError) as excinfo:
            BlobFetch(
                {
                    "url_field": "page_url",
                    "http": {"abuse_contact": "abuse@example.com", "fetch_reason": "contract test"},
                    "schema": {"mode": "fixed", "fields": ["id: str"]},
                }
            )

        assert "'page_url' (named by url_field)" in str(excinfo.value)

    def test_textract_names_key_field(self) -> None:
        from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis

        with pytest.raises(PluginConfigError) as excinfo:
            AWSTextractDocumentAnalysis(
                {
                    "region": "ap-southeast-2",
                    "feature_types": ["TABLES"],
                    "bucket": "example-bucket",
                    "text_field": "t",
                    "key_field": "s3_key",
                    "schema": {"mode": "fixed", "fields": ["id: str"]},
                }
            )

        assert "'s3_key' (named by key_field)" in str(excinfo.value)

    def test_azure_document_intelligence_names_source_field(self) -> None:
        with pytest.raises(PluginConfigError) as excinfo:
            AzureDocumentIntelligence(
                {
                    **_DI_BASE,
                    "schema": {"mode": "fixed", "fields": ["doc_id: str"]},
                }
            )

        assert "'doc_url' (named by source_field)" in str(excinfo.value)

    def test_rag_names_query_field(self) -> None:
        from elspeth.plugins.transforms.rag.transform import RAGRetrievalTransform

        with pytest.raises(PluginConfigError) as excinfo:
            RAGRetrievalTransform(
                {
                    "query_field": "q",
                    "output_prefix": "rag",
                    "provider": "chroma",
                    "provider_config": {"collection": "ragcoll", "persist_directory": "/tmp/rag-probe"},
                    "schema": {"mode": "fixed", "fields": ["id: str"]},
                }
            )

        assert "'q' (named by query_field)" in str(excinfo.value)

    def test_blob_csv_expand_defaulted_option_does_not_claim_required_input_fields(self) -> None:
        """The worst case: the author wrote NEITHER key that HEAD named.

        ``blob_ref_field`` is not in the config at all — its default supplies
        ``blob_ref`` — so no column-naming option can be credited, and the
        honest answer is a generic one. What must NOT happen is the message
        naming ``required_input_fields``, which is nowhere in this config.
        """
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

        with pytest.raises(PluginConfigError) as excinfo:
            BlobCSVExpand({"schema": {"mode": "fixed", "fields": ["id: str"]}})

        message = str(excinfo.value)
        provenance = message.split("A 'mode: fixed'")[0]
        assert "'blob_ref' (named by this transform's own options)" in message
        assert "required_input_fields" not in provenance

    def test_an_authored_required_input_field_still_names_that_option(self) -> None:
        """The one provenance HEAD got right must stay right.

        Narrowing the claim must not silence it: a name the author DID write in
        ``required_input_fields`` is still attributed there.
        """
        with pytest.raises(PluginConfigError) as excinfo:
            KeywordFilter(
                {
                    "fields": ["notes"],
                    "blocked_patterns": ["x"],
                    "required_input_fields": ["audit_id"],
                    "schema": {"mode": "fixed", "fields": ["notes: str"]},
                }
            )

        assert "'audit_id' (named by required_input_fields)" in str(excinfo.value)
