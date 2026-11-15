"""Azure AI Document Intelligence enrichment transform.

Submits a document reference (URL or base64) per row to Azure Document
Intelligence, waits for the asynchronous analysis (POST 202 + Operation-Location
poll), and enriches the row with extracted content and structured facets.
GA api-version 2024-11-30.

The submit/poll/retry/audit machinery is shared with azure_content_understanding
in ``analyze_lro``; this module owns the Document Intelligence request and
result shapes.

RETRY OWNERSHIP:
- A network error on the submit POST is a retryable row error: the engine
  retries the row, which submits the document again.
- A network error on an ``Operation-Location`` poll GET, after Azure accepted
  the analysis, is retried in place within the poll budget (like a 429/503),
  so it never resubmits the document or starts a second billed analysis;
  only one that outlasts ``poll_timeout_seconds`` escalates to a row retry.

SECURITY:
- A SAS token embedded in a ``urlSource`` value is forwarded to Azure verbatim;
  the audited request records the URL with its credential-shaped query values
  (``sig``) and userinfo fingerprinted.
- ``base64Source`` records the entire document verbatim in the audited request
  blob (~1.33x size). Operators with PII-sensitive documents should weigh their
  audit-retention policy.
- The polled ``Operation-Location`` URL carries our api-key header; we refuse to
  follow it unless its scheme is https and its host AND port match the configured
  ``endpoint``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Self, TypedDict
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import ContentTrust
from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.transforms.azure.analyze_lro import (
    AnalyzeRequest,
    AzureAnalyzeConfig,
    AzureAnalyzeTransform,
    validate_azure_ai_endpoint,
)
from elspeth.plugins.transforms.azure.analyze_result import AnalyzeStatus, operation_id_from_url, reported_id, warning_codes
from elspeth.plugins.transforms.azure.document_intelligence_result import (
    count_pages,
    extract_content,
    extract_facet_list,
    first_document_fields,
)
from elspeth.plugins.transforms.azure.errors import MalformedResponseError

_SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"2024-11-30"})

# config attr on ExtractFields -> Azure analyzeResult key
_FACET_AZURE_KEYS: dict[str, str] = {
    "pages": "pages",
    "tables": "tables",
    "key_value_pairs": "keyValuePairs",
    "paragraphs": "paragraphs",
    "documents": "documents",
    "languages": "languages",
    "styles": "styles",
    "figures": "figures",
    "sections": "sections",
}

_KNOWN_FEATURES: frozenset[str] = frozenset(
    {"ocrHighResolution", "languages", "barcodes", "formulas", "keyValuePairs", "styleFont", "queryFields"}
)

# Document Intelligence 2024-11-30 operation status -> service-neutral status.
# ``skipped`` is a batch-analysis state; on a single analyze it means no result
# was produced, which the row reports as a failed analysis.
_STATUS_MAP: dict[str, AnalyzeStatus] = {
    "notStarted": AnalyzeStatus.RUNNING,
    "running": AnalyzeStatus.RUNNING,
    "succeeded": AnalyzeStatus.SUCCEEDED,
    "failed": AnalyzeStatus.FAILED,
    "skipped": AnalyzeStatus.FAILED,
    "canceled": AnalyzeStatus.CANCELED,
}

_MODEL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._~-]{1,63}$")
_PAGES_PATTERN = re.compile(r"^(\d+(-\d+)?)(,\s*(\d+(-\d+)?))*$")
_AZURE_DOCUMENT_INTELLIGENCE_HOST_SUFFIXES: tuple[str, ...] = (
    ".cognitiveservices.azure.com",
    ".api.cognitive.microsoft.com",
    ".cognitiveservices.azure.us",
    ".api.cognitive.microsoft.us",
    ".cognitiveservices.azure.cn",
    ".api.cognitive.azure.cn",
)


def _validate_azure_document_intelligence_endpoint(value: str) -> str:
    """Validate that a credential-bearing endpoint targets Azure Document Intelligence."""
    return validate_azure_ai_endpoint(value, host_suffixes=_AZURE_DOCUMENT_INTELLIGENCE_HOST_SUFFIXES, service="Document Intelligence")


class DocumentIntelligenceMetadata(TypedDict):
    """Bounded analysis metadata (success_reason and ``metadata_field``)."""

    model_id: str
    api_version: str
    operation_id: str | None
    page_count: int
    document_count: int
    warning_count: int
    warning_codes: list[str]
    content_format: str
    features: list[str]
    result_status: str


class ExtractFields(BaseModel):
    """Facet -> output-field-name map. Only set facets are emitted as row fields.

    ``figures`` reads ``analyzeResult.figures`` metadata (regions/spans), which is
    distinct from the ``output=figures`` artifact-generation option (out of scope).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: str | None = None
    tables: str | None = None
    key_value_pairs: str | None = None
    paragraphs: str | None = None
    documents: str | None = None
    languages: str | None = None
    styles: str | None = None
    figures: str | None = None
    sections: str | None = None


class AzureDocumentIntelligenceConfig(AzureAnalyzeConfig):
    """Configuration for the azure_document_intelligence transform."""

    endpoint: str = Field(..., description="Azure Document Intelligence resource endpoint URL (HTTPS, Azure host only).")
    api_version: str = Field("2024-11-30", description="REST api-version (GA 2024-11-30 only in v1).")
    model_id: str = Field(..., description="Prebuilt or custom model id, e.g. prebuilt-layout, prebuilt-invoice.")

    content_field: str | None = Field(None, description="Row field for the full text/markdown content.")
    output_content_format: str = Field("text", pattern=r"^(text|markdown)$", description="outputContentFormat: text or markdown.")
    extract: ExtractFields = Field(default_factory=ExtractFields, description="Facet -> output-field-name map.")
    page_count_field: str | None = Field(None, description="Row field for the analyzed page count (int).")
    fields_field: str | None = Field(
        None,
        description=(
            "Row field for the first document's extracted fields as plain values ({name: value}); "
            "for typed models (prebuilt-invoice, custom) and queryFields."
        ),
    )
    metadata_field: str | None = Field(
        None, description="Row field for bounded analysis metadata (model id, api version, operation id, counts, warning codes)."
    )
    result_field: str | None = Field(
        None,
        description=(
            "Row field for the entire analyzeResult object (max fidelity). Stores the full "
            "result (~5-50 MB for large documents) as a row value; prefer content_field or "
            "specific extract facets for normal use."
        ),
    )

    pages: str | None = Field(None, description="1-based page range, e.g. '1-3,5,7-9'.")
    locale: str | None = Field(None, description="Locale hint, e.g. 'en-US'.")
    string_index_type: str = Field(
        "textElements",
        pattern=r"^(textElements|unicodeCodePoint|utf16CodeUnit)$",
        description="stringIndexType: textElements, unicodeCodePoint, or utf16CodeUnit.",
    )
    features: list[str] = Field(default_factory=list, description="Add-on analysis features.")
    query_fields: list[str] = Field(default_factory=list, description="Additional fields to extract (requires queryFields feature).")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        return _validate_azure_document_intelligence_endpoint(v)

    @field_validator("api_version")
    @classmethod
    def _validate_api_version(cls, v: str) -> str:
        if v not in _SUPPORTED_API_VERSIONS:
            raise ValueError(f"api_version {v!r} is not supported. Supported: {sorted(_SUPPORTED_API_VERSIONS)}.")
        return v

    @field_validator("model_id")
    @classmethod
    def _validate_model_id(cls, v: str) -> str:
        if not _MODEL_ID_PATTERN.match(v):
            raise ValueError(f"model_id {v!r} must match {_MODEL_ID_PATTERN.pattern} (Azure model id rules).")
        return v

    @field_validator("pages")
    @classmethod
    def _validate_pages(cls, v: str | None) -> str | None:
        if v is not None and not _PAGES_PATTERN.match(v):
            raise ValueError(f"pages {v!r} must match {_PAGES_PATTERN.pattern} (e.g. '1-3,5').")
        return v

    @field_validator("features")
    @classmethod
    def _validate_features(cls, v: list[str]) -> list[str]:
        unknown = [feature for feature in v if feature not in _KNOWN_FEATURES]
        if unknown:
            raise ValueError(f"Unknown features {unknown}. Known: {sorted(_KNOWN_FEATURES)}.")
        return v

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        has_qf_feature = "queryFields" in self.features
        if bool(self.query_fields) != has_qf_feature:
            raise ValueError("query_fields and the 'queryFields' feature must be set together (both or neither).")
        return self

    def output_target_description(self) -> str:
        return "content_field, page_count_field, fields_field, metadata_field, result_field, or one entry in extract"

    def configured_output_fields(self) -> dict[str, str]:
        """Map Azure analyzeResult facet key -> output row field, for set extract entries only."""
        result: dict[str, str] = {}
        for attr, azure_key in _FACET_AZURE_KEYS.items():
            field_name = getattr(self.extract, attr)
            if field_name is not None:
                result[azure_key] = field_name
        return result

    def all_output_field_names(self) -> list[str]:
        """Every output row field this transform may add (order: content, page_count, fields, metadata, result, facets)."""
        names: list[str] = []
        for name in (self.content_field, self.page_count_field, self.fields_field, self.metadata_field, self.result_field):
            if name is not None:
                names.append(name)
        names.extend(self.configured_output_fields().values())
        return names


class AzureDocumentIntelligence(AzureAnalyzeTransform):
    """Enrich rows with Azure Document Intelligence extraction (async analyze LRO).

    Each row supplies one document reference (URL or base64); the transform submits
    it, polls the long-running operation to completion, and adds the configured
    extraction facets to the row. Uses row-level pipelining (BatchTransformMixin):
    multiple rows are in flight concurrently with FIFO output ordering.
    """

    # source_field is the INPUT column (holds the document URL/base64); these
    # name the row fields the analysis result is WRITTEN to.
    output_naming_config_keys = frozenset({"content_field", "page_count_field", "fields_field", "metadata_field", "result_field"})
    name = "azure_document_intelligence"
    service_label = "Azure Document Intelligence"
    _POLL_STATUSES = _STATUS_MAP
    _RESULT_KEY = "analyzeResult"
    _MISSING_RESULT_ERROR = "missing_analyze_result"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    # Placeholder must be a sha256: literal so the hash normalizer matches it; recomputed by scripts/cicd/plugin_hash.
    source_file_hash: str | None = "sha256:af3c39691fe2f540"
    config_model = AzureDocumentIntelligenceConfig
    passes_through_input = True
    content_trust = ContentTrust.UNTRUSTED
    creates_tokens = False
    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {
        "api_key": ("AZURE_DOCUMENT_INTELLIGENCE_KEY",),
    }
    audit_characteristics = frozenset({AuditCharacteristic.CREDENTIALS})
    capability_tags = ("azure", "document", "ocr", "enrichment", "http")

    usage_when_to_use = (
        "Use URL or base64 mode to extract text, tables, key-value pairs, and typed fields from documents. "
        "Request audit retains the submitted URL (SAS signatures fingerprinted) or encoded body; extracted "
        "remote content is untrusted before LLM consumption."
    )
    usage_when_not_to_use = (
        "Not for plain web pages or safety screening. Never embed a credential in a document URL, and "
        "account for the request audit's credential-exposure and data-retention implications; use web_scrape "
        "for public HTML pages."
    )
    example_use = (
        "transform:\n"
        "  plugin: azure_document_intelligence\n"
        "  options:\n"
        "    endpoint: https://catalogue-di.cognitiveservices.azure.com\n"
        "    api_key: {secret_ref: AZURE_DOCUMENT_INTELLIGENCE_KEY}\n"
        "    model_id: prebuilt-layout\n"
        "    source_mode: url\n"
        "    source_field: document_url\n"
        "    content_field: di_content\n"
        "    output_content_format: markdown\n"
        "    features: [keyValuePairs]\n"
        "    extract: {tables: di_tables, key_value_pairs: di_kv}\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 forward invariant (no network/credentials)."""
        return {
            "endpoint": "https://test.cognitiveservices.azure.com",
            "api_key": "test-key",
            "model_id": "prebuilt-layout",
            "source_mode": "url",
            "source_field": "doc_intelligence_probe_url",
            "content_field": "di_content",
            "schema": {"mode": "observed"},
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = AzureDocumentIntelligenceConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._cfg = cfg
        self._init_analyze(cfg, endpoint=cfg.endpoint)

        self._api_version = cfg.api_version
        self._model_id = cfg.model_id
        self._content_field = cfg.content_field
        self._output_content_format = cfg.output_content_format
        self._page_count_field = cfg.page_count_field
        self._fields_field = cfg.fields_field
        self._metadata_field = cfg.metadata_field
        self._result_field = cfg.result_field
        self._facet_fields = cfg.configured_output_fields()  # azure_key -> row field

        self.declared_output_fields = frozenset(cfg.all_output_field_names())
        # The plugin's declaration of every field it creates (ADR-050):
        # ``extract_content`` returns the document text (``str``) and
        # ``count_pages`` the validated page count (``int``); the raw analyze
        # result, the field map and the metadata are mappings and every facet a
        # list, which the schema DSL has no scalar type for (``any``).
        created: list[FieldDefinition] = []
        if cfg.content_field is not None:
            created.append(FieldDefinition(cfg.content_field, "str"))
        if cfg.page_count_field is not None:
            created.append(FieldDefinition(cfg.page_count_field, "int"))
        for mapping_field in (cfg.fields_field, cfg.metadata_field, cfg.result_field):
            if mapping_field is not None:
                created.append(FieldDefinition(mapping_field, "any"))
        created.extend(FieldDefinition(name, "any") for name in self._facet_fields.values())
        self._created_output_fields: tuple[FieldDefinition, ...] = tuple(created)
        self._reject_input_options_naming_created_fields({"source_field": cfg.source_field})

        schema_config = cfg.schema_config
        self.input_schema = create_schema_from_config(schema_config, "AzureDocumentIntelligenceInput", allow_coercion=False)
        self._output_schema_config = self._build_output_schema_config(schema_config)
        self.output_schema = create_schema_from_config(self._output_schema_config, "AzureDocumentIntelligenceOutput", allow_coercion=False)

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """The configured output targets, typed as the result parsers fix them (ADR-050)."""
        return self._created_output_fields

    # ── Service hooks ─────────────────────────────────────────────────────

    def _analyze_url(self) -> str:
        params: list[tuple[str, str]] = [("_overload", "analyzeDocument"), ("api-version", self._api_version)]
        if self._output_content_format != "text":
            params.append(("outputContentFormat", self._output_content_format))
        if self._cfg.pages is not None:
            params.append(("pages", self._cfg.pages))
        if self._cfg.locale is not None:
            params.append(("locale", self._cfg.locale))
        params.append(("stringIndexType", self._cfg.string_index_type))
        if self._cfg.features:
            params.append(("features", ",".join(self._cfg.features)))
        if self._cfg.query_fields:
            params.append(("queryFields", ",".join(self._cfg.query_fields)))
        query = urlencode(params)
        return f"{self._endpoint}/documentintelligence/documentModels/{self._model_id}:analyze?{query}"

    def _url_request(self, document_url: str, audit_url: str) -> AnalyzeRequest:
        return AnalyzeRequest(body={"urlSource": document_url}, audit_body={"urlSource": audit_url})

    def _base64_request(self, data: str) -> AnalyzeRequest:
        body = {"base64Source": data}
        return AnalyzeRequest(body=body, audit_body=body)

    def _build_enriched_result(self, row: PipelineRow, result: Mapping[str, Any], *, operation_url: str) -> TransformResult:
        # ALL Tier-3 reads of the result must stay inside this guard so a malformed
        # response fails the row closed rather than raising past the executor (which would
        # cancel the whole batch). Counts are computed once here and reused in the
        # success metadata below — never re-parsed outside the guard.
        try:
            output = row.to_dict()
            page_count = count_pages(result)
            warnings = warning_codes(result)
            document_count = len(extract_facet_list(result, "documents"))
            if self._content_field is not None:
                output[self._content_field] = extract_content(result)
            if self._page_count_field is not None:
                output[self._page_count_field] = page_count
            if self._fields_field is not None:
                output[self._fields_field] = first_document_fields(result)
            if self._result_field is not None:
                output[self._result_field] = dict(result)
            for azure_key, field_name in self._facet_fields.items():
                output[field_name] = extract_facet_list(result, azure_key)
        except MalformedResponseError as e:
            return TransformResult.error({"reason": "malformed_response", "message": str(e)}, retryable=False)

        reported_model = reported_id(result.get("modelId"), _MODEL_ID_PATTERN)
        metadata: DocumentIntelligenceMetadata = {
            "model_id": reported_model if reported_model is not None else self._model_id,
            "api_version": self._api_version,
            "operation_id": operation_id_from_url(operation_url, collection="analyzeResults"),
            "page_count": page_count,
            "document_count": document_count,
            "warning_count": len(warnings),
            "warning_codes": warnings,
            "content_format": self._output_content_format,
            "features": list(self._cfg.features),
            "result_status": "succeeded",
        }
        if self._metadata_field is not None:
            output[self._metadata_field] = dict(metadata)

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output)
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={"action": "enriched", "fields_added": sorted(self.declared_output_fields), "metadata": dict(metadata)},
        )

    def _forward_invariant_probe_bodies(self) -> tuple[str, Mapping[str, Any]]:
        op_url = (
            f"{self._endpoint}/documentintelligence/documentModels/{self._model_id}/analyzeResults/probe?api-version={self._api_version}"
        )
        return op_url, {"status": "succeeded", "analyzeResult": {"content": "probe"}}

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary=(
                    "Enrich rows with Azure Document Intelligence: submit a document URL or base64 string "
                    "and add extracted content (text/markdown) plus facets (pages, tables, key-value pairs, "
                    "paragraphs, typed model documents) and plain typed field values. Async analyze, fully audited."
                ),
                composer_hints=(
                    "source_mode chooses how the document is read: 'url' (a reachable/SAS URL in source_field) or 'base64'.",
                    "model_id selects extraction: prebuilt-read/layout/document, prebuilt-invoice/receipt/idDocument/tax, or a custom model id.",
                    "Configure at least one output: content_field, fields_field, metadata_field and/or extract.{tables,key_value_pairs,documents,...}; each becomes a row field.",
                    "fields_field gives the first document's typed fields as plain values (e.g. InvoiceTotal), the simplest input for routing or an LLM.",
                    "output_content_format: markdown gives section-structured content; text gives plain text.",
                    "Extraction is external and non-deterministic; route on_error to a quarantine sink. Malformed Azure responses fail closed.",
                ),
            )
        return None
