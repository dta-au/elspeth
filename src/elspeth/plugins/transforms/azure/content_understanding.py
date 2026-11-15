"""Azure AI Content Understanding enrichment transform.

Submits a document (or image/audio/video) reference per row to an Azure AI
Content Understanding analyzer, waits for the asynchronous analysis (POST 202 +
Operation-Location poll), and enriches the row with the analyzer's markdown,
extracted fields and structural facets. GA api-version 2025-11-01.

The submit/poll/retry/audit machinery is shared with azure_document_intelligence
in ``analyze_lro``; this module owns the Content Understanding request and
result shapes.

SECURITY:
- A SAS token embedded in an input ``url`` is forwarded to Azure verbatim; the
  audited request records the URL with its credential-shaped query values
  (``sig``) and userinfo fingerprinted.
- Inline ``data`` (base64) records the entire file verbatim in the audited
  request blob (~1.33x size). Operators with PII-sensitive content should weigh
  their audit-retention policy.
- The polled ``Operation-Location`` URL carries our api-key header; we refuse to
  follow it unless its scheme is https and its host AND port match the configured
  ``endpoint``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
from elspeth.plugins.transforms.azure.content_understanding_result import parse_result_contents
from elspeth.plugins.transforms.azure.errors import MalformedResponseError

_SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"2025-11-01"})

# Content Understanding 2025-11-01 OperationState -> service-neutral status.
_STATUS_MAP: dict[str, AnalyzeStatus] = {
    "NotStarted": AnalyzeStatus.RUNNING,
    "Running": AnalyzeStatus.RUNNING,
    "Succeeded": AnalyzeStatus.SUCCEEDED,
    "Failed": AnalyzeStatus.FAILED,
    "Canceled": AnalyzeStatus.CANCELED,
}

_ANALYZER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
# Document ranges are 1-based pages, audio/video ranges integer milliseconds;
# an open-ended ``9-`` runs to the end.
_RANGE_PATTERN = re.compile(r"^\d+(-\d*)?(,\d+(-\d*)?)*$")
_MIME_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$")
_DEPLOYMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_MODEL_DEPLOYMENTS = 16
_AZURE_CONTENT_UNDERSTANDING_HOST_SUFFIXES: tuple[str, ...] = (
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
)


def _validate_azure_content_understanding_endpoint(value: str) -> str:
    """Validate that a credential-bearing endpoint targets an Azure AI Foundry / AI Services resource."""
    return validate_azure_ai_endpoint(value, host_suffixes=_AZURE_CONTENT_UNDERSTANDING_HOST_SUFFIXES, service="Content Understanding")


class ContentUnderstandingMetadata(TypedDict):
    """Bounded analysis metadata (success_reason and ``metadata_field``)."""

    analyzer_id: str
    api_version: str
    operation_id: str | None
    content_count: int
    page_count: int
    warning_count: int
    warning_codes: list[str]
    result_status: str


class ContentUnderstandingExtractFields(BaseModel):
    """Facet -> output-field-name map. Each facet is concatenated across result contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contents: str | None = None
    pages: str | None = None
    tables: str | None = None
    paragraphs: str | None = None
    sections: str | None = None
    figures: str | None = None
    hyperlinks: str | None = None
    transcript_phrases: str | None = None


class AzureContentUnderstandingConfig(AzureAnalyzeConfig):
    """Configuration for the azure_content_understanding transform."""

    endpoint: str = Field(
        ...,
        description="Azure AI Foundry / AI Services resource endpoint URL (HTTPS, *.services.ai.azure.com or *.cognitiveservices.azure.com).",
    )
    api_version: str = Field("2025-11-01", description="REST api-version (GA 2025-11-01).")
    analyzer_id: str = Field(
        ..., description="Prebuilt or custom analyzer id, e.g. prebuilt-documentSearch, prebuilt-invoice, my-analyzer."
    )
    content_range: str | None = Field(
        None, description="Range of the input to analyze: 1-based pages for documents ('1-3,5,9-'), milliseconds for audio/video."
    )
    mime_type: str | None = Field(None, description="MIME type sent with each input, e.g. application/pdf (most useful in base64 mode).")
    processing_location: str | None = Field(
        None, pattern=r"^(geography|dataZone|global)$", description="Where generative processing may run: geography, dataZone, or global."
    )
    string_encoding: str | None = Field(
        None, pattern=r"^(codePoint|utf16|utf8)$", description="Span offset encoding: codePoint (service default), utf16, or utf8."
    )
    model_deployments: dict[str, str] = Field(
        default_factory=dict, description="Per-request model -> deployment overrides for generative analyzers (else resource defaults)."
    )

    content_field: str | None = Field(None, description="Row field for the markdown of every analyzed content, joined by a blank line.")
    fields_field: str | None = Field(None, description="Row field for the analyzer's extracted fields as plain values ({name: value}).")
    page_count_field: str | None = Field(None, description="Row field for the number of document pages analyzed (int).")
    metadata_field: str | None = Field(
        None, description="Row field for bounded analysis metadata (analyzer id, api version, operation id, counts, warning codes)."
    )
    result_field: str | None = Field(
        None,
        description=(
            "Row field for the entire analysis result object (max fidelity, can be large); prefer "
            "content_field, fields_field or specific extract facets for normal use."
        ),
    )
    extract: ContentUnderstandingExtractFields = Field(
        default_factory=ContentUnderstandingExtractFields, description="Facet -> output-field-name map."
    )

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, v: str) -> str:
        return _validate_azure_content_understanding_endpoint(v)

    @field_validator("api_version")
    @classmethod
    def _validate_api_version(cls, v: str) -> str:
        if v not in _SUPPORTED_API_VERSIONS:
            raise ValueError(f"api_version {v!r} is not supported. Supported: {sorted(_SUPPORTED_API_VERSIONS)}.")
        return v

    @field_validator("analyzer_id")
    @classmethod
    def _validate_analyzer_id(cls, v: str) -> str:
        if not _ANALYZER_ID_PATTERN.match(v):
            raise ValueError(f"analyzer_id {v!r} must match {_ANALYZER_ID_PATTERN.pattern} (Azure analyzer id rules).")
        return v

    @field_validator("content_range")
    @classmethod
    def _validate_content_range(cls, v: str | None) -> str | None:
        if v is not None and not _RANGE_PATTERN.match(v):
            raise ValueError(f"content_range {v!r} must match {_RANGE_PATTERN.pattern} (e.g. '1-3,5,9-').")
        return v

    @field_validator("mime_type")
    @classmethod
    def _validate_mime_type(cls, v: str | None) -> str | None:
        if v is not None and not _MIME_TYPE_PATTERN.match(v):
            raise ValueError(f"mime_type {v!r} must be a lowercase type/subtype, e.g. application/pdf.")
        return v

    @field_validator("model_deployments")
    @classmethod
    def _validate_model_deployments(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_MODEL_DEPLOYMENTS:
            raise ValueError(f"model_deployments allows at most {_MAX_MODEL_DEPLOYMENTS} entries.")
        bad = sorted(
            name
            for name, deployment in v.items()
            if not _DEPLOYMENT_NAME_PATTERN.match(name) or not _DEPLOYMENT_NAME_PATTERN.match(deployment)
        )
        if bad:
            raise ValueError(f"model_deployments entries must map a model name to a deployment name; invalid: {bad}.")
        return v

    def output_target_description(self) -> str:
        return "content_field, fields_field, page_count_field, metadata_field, result_field, or one entry in extract"

    def configured_output_fields(self) -> dict[str, str]:
        """Map Content Understanding content key -> output row field, for set extract entries only."""
        facets = {
            "contents": self.extract.contents,
            "pages": self.extract.pages,
            "tables": self.extract.tables,
            "paragraphs": self.extract.paragraphs,
            "sections": self.extract.sections,
            "figures": self.extract.figures,
            "hyperlinks": self.extract.hyperlinks,
            "transcriptPhrases": self.extract.transcript_phrases,
        }
        return {azure_key: field_name for azure_key, field_name in facets.items() if field_name is not None}

    def all_output_field_names(self) -> list[str]:
        """Every output row field this transform may add (order: content, fields, page_count, metadata, result, facets)."""
        names: list[str] = []
        for name in (self.content_field, self.fields_field, self.page_count_field, self.metadata_field, self.result_field):
            if name is not None:
                names.append(name)
        names.extend(self.configured_output_fields().values())
        return names


class AzureContentUnderstanding(AzureAnalyzeTransform):
    """Enrich rows with Azure AI Content Understanding analysis (async analyze LRO).

    Each row supplies one file reference (URL or base64); the transform submits it
    to the configured analyzer, polls the long-running operation to completion,
    and adds the configured outputs to the row. Uses row-level pipelining
    (BatchTransformMixin): multiple rows are in flight concurrently with FIFO
    output ordering.
    """

    # source_field is the INPUT column (holds the file URL/base64); these name
    # the row fields the analysis result is WRITTEN to.
    output_naming_config_keys = frozenset({"content_field", "fields_field", "page_count_field", "metadata_field", "result_field"})
    name = "azure_content_understanding"
    service_label = "Azure Content Understanding"
    _POLL_STATUSES = _STATUS_MAP
    _RESULT_KEY = "result"
    _MISSING_RESULT_ERROR = "missing_result"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    # Placeholder must be a sha256: literal so the hash normalizer matches it; recomputed by scripts/cicd/plugin_hash.
    source_file_hash: str | None = "sha256:317412245440ee01"
    config_model = AzureContentUnderstandingConfig
    passes_through_input = True
    content_trust = ContentTrust.UNTRUSTED
    creates_tokens = False
    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {
        "api_key": ("AZURE_CONTENT_UNDERSTANDING_KEY",),
    }
    audit_characteristics = frozenset({AuditCharacteristic.CREDENTIALS})
    capability_tags = ("azure", "document", "multimodal", "extraction", "http")

    usage_when_to_use = (
        "Use URL or base64 mode to run a Content Understanding analyzer over a document, image, audio or "
        "video file and add its markdown and schema-defined fields to the row. Request audit retains the "
        "submitted URL (SAS signatures fingerprinted) or encoded body; extracted remote content is "
        "untrusted before LLM consumption."
    )
    usage_when_not_to_use = (
        "Not for plain web pages or safety screening; generative analyzers need model deployments. Never embed "
        "a credential in a file URL, and account for the request audit's credential-exposure and data-retention "
        "implications."
    )
    example_use = (
        "transform:\n"
        "  plugin: azure_content_understanding\n"
        "  options:\n"
        "    endpoint: https://catalogue-ai.services.ai.azure.com\n"
        "    api_key: {secret_ref: AZURE_CONTENT_UNDERSTANDING_KEY}\n"
        "    analyzer_id: prebuilt-invoice\n"
        "    source_mode: url\n"
        "    source_field: document_url\n"
        "    content_field: cu_markdown\n"
        "    fields_field: cu_fields\n"
        "    extract: {tables: cu_tables}\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 forward invariant (no network/credentials)."""
        return {
            "endpoint": "https://test.services.ai.azure.com",
            "api_key": "test-key",
            "analyzer_id": "prebuilt-documentSearch",
            "source_mode": "url",
            "source_field": "content_understanding_probe_url",
            "content_field": "cu_markdown",
            "schema": {"mode": "observed"},
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = AzureContentUnderstandingConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._cfg = cfg
        self._init_analyze(cfg, endpoint=cfg.endpoint)

        self._api_version = cfg.api_version
        self._analyzer_id = cfg.analyzer_id
        self._content_field = cfg.content_field
        self._fields_field = cfg.fields_field
        self._page_count_field = cfg.page_count_field
        self._metadata_field = cfg.metadata_field
        self._result_field = cfg.result_field
        self._facet_fields = cfg.configured_output_fields()  # content key -> row field

        self.declared_output_fields = frozenset(cfg.all_output_field_names())
        # The plugin's declaration of every field it creates (ADR-050):
        # ``ResultContents.markdown`` returns ``str`` and ``page_count`` a
        # validated ``int``; the field map, metadata and raw result are
        # mappings and every facet a list, which the schema DSL has no scalar
        # type for (``any``).
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
        self.input_schema = create_schema_from_config(schema_config, "AzureContentUnderstandingInput", allow_coercion=False)
        self._output_schema_config = self._build_output_schema_config(schema_config)
        self.output_schema = create_schema_from_config(self._output_schema_config, "AzureContentUnderstandingOutput", allow_coercion=False)

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """The configured output targets, typed as the result parsers fix them (ADR-050)."""
        return self._created_output_fields

    # ── Service hooks ─────────────────────────────────────────────────────

    def _analyze_url(self) -> str:
        params: list[tuple[str, str]] = [("api-version", self._api_version)]
        if self._cfg.string_encoding is not None:
            params.append(("stringEncoding", self._cfg.string_encoding))
        if self._cfg.processing_location is not None:
            params.append(("processingLocation", self._cfg.processing_location))
        query = urlencode(params)
        analyzer = quote(self._analyzer_id, safe="")
        return f"{self._endpoint}/contentunderstanding/analyzers/{analyzer}:analyze?{query}"

    def _input(self, source: dict[str, str]) -> dict[str, str]:
        item = dict(source)
        if self._cfg.mime_type is not None:
            item["mimeType"] = self._cfg.mime_type
        if self._cfg.content_range is not None:
            item["range"] = self._cfg.content_range
        return item

    def _request(self, wire_input: dict[str, str], audit_input: dict[str, str]) -> AnalyzeRequest:
        extra: dict[str, dict[str, str]] = {}
        if self._cfg.model_deployments:
            extra["modelDeployments"] = dict(self._cfg.model_deployments)
        return AnalyzeRequest(body={"inputs": [wire_input], **extra}, audit_body={"inputs": [audit_input], **extra})

    def _url_request(self, document_url: str, audit_url: str) -> AnalyzeRequest:
        return self._request(self._input({"url": document_url}), self._input({"url": audit_url}))

    def _base64_request(self, data: str) -> AnalyzeRequest:
        item = self._input({"data": data})
        return self._request(item, item)

    def _build_enriched_result(self, row: PipelineRow, result: Mapping[str, Any], *, operation_url: str) -> TransformResult:
        # ALL Tier-3 reads of the result stay inside this guard so a malformed
        # response fails the row closed rather than raising past the executor
        # (which would cancel the whole batch).
        try:
            output = row.to_dict()
            contents = parse_result_contents(result)
            page_count = contents.page_count
            warnings = warning_codes(result)
            if self._content_field is not None:
                output[self._content_field] = contents.markdown()
            if self._fields_field is not None:
                output[self._fields_field] = contents.first_fields()
            if self._page_count_field is not None:
                output[self._page_count_field] = page_count
            if self._result_field is not None:
                output[self._result_field] = dict(result)
            for azure_key, field_name in self._facet_fields.items():
                output[field_name] = contents.as_list() if azure_key == "contents" else contents.facet(azure_key)
        except MalformedResponseError as e:
            return TransformResult.error({"reason": "malformed_response", "message": str(e)}, retryable=False)

        reported_analyzer = reported_id(result.get("analyzerId"), _ANALYZER_ID_PATTERN)
        metadata: ContentUnderstandingMetadata = {
            "analyzer_id": reported_analyzer if reported_analyzer is not None else self._analyzer_id,
            "api_version": self._api_version,
            "operation_id": operation_id_from_url(operation_url, collection="analyzerResults"),
            "content_count": len(contents),
            "page_count": page_count,
            "warning_count": len(warnings),
            "warning_codes": warnings,
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
        op_url = f"{self._endpoint}/contentunderstanding/analyzerResults/probe?api-version={self._api_version}"
        content = {"kind": "document", "mimeType": "application/pdf", "markdown": "probe", "startPageNumber": 1, "endPageNumber": 1}
        return op_url, {"id": "probe", "status": "Succeeded", "result": {"analyzerId": self._analyzer_id, "contents": [content]}}

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary=(
                    "Enrich rows with Azure AI Content Understanding: submit a document, image, audio or video "
                    "URL (or base64) to an analyzer and add its markdown, schema-defined fields and structural "
                    "facets. Async analyze, fully audited."
                ),
                composer_hints=(
                    "source_mode chooses how the file is read: 'url' (a reachable/SAS URL in source_field) or 'base64'.",
                    "analyzer_id selects the analyzer: prebuilt-read/layout/documentSearch, prebuilt-invoice/receipt/idDocument, prebuilt-audioSearch/videoSearch, or a custom analyzer id.",
                    "Configure at least one output: content_field (markdown), fields_field (extracted fields as plain values), metadata_field and/or extract.{tables,pages,...}.",
                    "Generative analyzers need model deployments on the resource (or model_deployments); prebuilt-read and prebuilt-layout do not.",
                    "Analysis is external and non-deterministic; route on_error to a quarantine sink. Malformed Azure responses fail closed.",
                ),
            )
        return None
