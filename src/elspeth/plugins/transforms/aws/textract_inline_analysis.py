"""Synchronous Amazon Textract inline-analysis transform.

Analyzes payload-store documents (managed-blob bytes) through one audited
``AnalyzeDocument`` call per row. The asynchronous S3 sibling is
``aws_textract_document_analysis``; the two share one configuration
vocabulary, credential contract, SDK builder, and pure result parser, and
emit identical normalized output shapes so downstream projection logic can
switch between them unchanged (elspeth-0c6a343921).
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, ClassVar, Self, cast

import structlog
from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.binary_documents import (
    BINARY_DOCUMENT_MAX_BYTES,
    BinaryDocumentFormat,
    binary_document_signature_matches,
)
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import FrameworkBugError, TransformErrorCategory
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.batching import BatchTransformMixin, OutputPort
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.telemetry import make_warn_telemetry_before_start
from elspeth.plugins.transforms.aws.textract_client import (
    SDK_TOTAL_MAX_ATTEMPTS,
    TextractInlineClient,
    TextractResponseError,
    TextractServiceError,
    TextractSyncSDKClient,
    build_textract_sync_sdk_client,
)
from elspeth.plugins.transforms.aws.textract_config_shared import (
    FEATURE_TYPES,
    AuthMode,
    FeatureType,
    TextractExtractFields,
    TextractQueryConfig,
    require_non_whitespace,
    validate_textract_credential_fields,
)
from elspeth.plugins.transforms.aws.textract_regions import TEXTRACT_INVARIANT_PROBE_REGION, TEXTRACT_REGIONS
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    NormalizedTextractResult,
    normalize_analyze_document_result,
)

_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_INLINE_QUERIES = 15
_TEXTRACT_SDK_CONNECT_TIMEOUT_SECONDS = 10.0
_TEXTRACT_SDK_TIMEOUT_HEADROOM_SECONDS = 90.0

logger = structlog.get_logger(__name__)
_warn_telemetry_before_start = make_warn_telemetry_before_start(logger)


class TextractInlineQueryConfig(TextractQueryConfig):
    """One query restricted to the synchronous single-page selectors."""

    @field_validator("pages")
    @classmethod
    def _single_page_selectors(cls, selectors: list[str]) -> list[str]:
        if selectors not in ([], ["1"], ["*"]):
            raise ValueError('pages must be absent, ["1"], or ["*"] for the single-page inline plugin')
        return selectors


class AWSTextractInlineAnalysisConfig(TransformDataConfig):
    """Validated public configuration for synchronous inline document analysis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        **TransformDataConfig.model_config,
        hide_input_in_errors=True,
    )

    region: str = Field(min_length=1, max_length=64, description="AWS region providing the Amazon Textract endpoint.")
    auth_mode: AuthMode = Field(
        default="default_chain",
        description="Use boto3's default credential chain or credentials resolved from ELSPETH secret references.",
    )
    aws_access_key_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        repr=False,
        description="Resolved AWS access-key identifier; accepted only with auth_mode secret_refs.",
    )
    aws_secret_access_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        repr=False,
        description="Resolved AWS secret access key; accepted only with auth_mode secret_refs.",
    )
    aws_session_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=16384,
        repr=False,
        description="Optional resolved AWS session token for temporary secret_refs credentials.",
    )

    blob_ref_field: str = Field(
        default="blob_ref",
        min_length=1,
        max_length=256,
        description="Input row field containing the payload-store SHA-256 content hash of the document bytes.",
    )
    document_format: BinaryDocumentFormat = Field(
        description="Declared document format (jpeg, png, or pdf) for every row; runtime verifies the exact byte signature.",
    )
    feature_types: list[FeatureType] = Field(
        min_length=1,
        max_length=5,
        description="Unique Textract analysis features selected from TABLES, FORMS, QUERIES, SIGNATURES, and LAYOUT.",
    )
    queries: list[TextractInlineQueryConfig] = Field(
        default_factory=list,
        max_length=_MAX_INLINE_QUERIES,
        description="Single-page Textract query definitions; configured together with the QUERIES feature.",
    )

    text_field: str | None = Field(default=None, description="Optional output field for page-ordered LINE text.")
    page_count_field: str | None = Field(
        default=None,
        description="Optional output field for the validated document page count, always 1 on success.",
    )
    metadata_field: str | None = Field(
        default=None,
        description="Optional output field for bounded document and model metadata.",
    )
    extract: TextractExtractFields = Field(
        default_factory=TextractExtractFields,
        description="Optional mappings from normalized document facets to output row fields.",
    )
    result_field: str | None = Field(default=None, description="Optional output field for the bounded provider-shaped aggregate.")

    max_document_bytes: int = Field(
        default=BINARY_DOCUMENT_MAX_BYTES,
        gt=0,
        le=BINARY_DOCUMENT_MAX_BYTES,
        description="Maximum accepted document byte length; may be reduced but never raised above the 5 MiB provider bound.",
    )
    max_blocks: int = Field(default=100_000, gt=0, description="Maximum Textract block count for one document.")
    max_result_bytes: int = Field(default=50_000_000, gt=0, description="Maximum canonical JSON bytes retained for one document result.")
    request_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="SDK read timeout for one AnalyzeDocument attempt; the connect timeout remains 10 seconds.",
    )
    batch_wait_timeout_seconds: float = Field(default=420.0, gt=0, description="Maximum engine wait for one pipelined document row.")

    @field_validator("request_timeout_seconds", "batch_wait_timeout_seconds")
    @classmethod
    def _finite_timing(cls, value: float, info: ValidationInfo) -> float:
        if not math.isfinite(value):
            field_name = info.field_name or "timing value"
            raise ValueError(f"{field_name} must be finite")
        return value

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        if value not in TEXTRACT_REGIONS:
            raise ValueError("region must be a supported Amazon Textract region")
        return value

    @field_validator("blob_ref_field", "text_field", "page_count_field", "metadata_field", "result_field")
    @classmethod
    def _field_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        field_name = info.field_name or "field"
        return require_non_whitespace(value, field_name=field_name)

    @field_validator("feature_types")
    @classmethod
    def _features(cls, value: list[FeatureType]) -> list[FeatureType]:
        if any(feature not in FEATURE_TYPES for feature in value):
            raise ValueError(f"feature_types must use only {sorted(FEATURE_TYPES)}")
        if len(set(value)) != len(value):
            raise ValueError("feature_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        for facet_name, facet_value in self.extract.facet_items():
            require_non_whitespace(facet_value, field_name=f"extract.{facet_name}")

        has_queries = bool(self.queries)
        has_query_feature = "QUERIES" in self.feature_types
        if has_queries != has_query_feature:
            raise ValueError("queries and the QUERIES feature must be configured together")

        validate_textract_credential_fields(
            auth_mode=self.auth_mode,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
        )

        names = self.all_output_field_names()
        if not names:
            raise ValueError(
                "At least one output target must be configured: text_field, page_count_field, metadata_field, result_field, or extract"
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate output field names: {duplicates}. Each output field must be unique")
        return self

    def configured_output_fields(self) -> dict[str, str]:
        """Map configured normalized facet names to their output-row fields."""
        return {facet_name: field_name for facet_name, field_name in self.extract.facet_items() if field_name is not None}

    def all_output_field_names(self) -> list[str]:
        """Return every configured output row field in stable projection order."""
        names = [name for name in (self.text_field, self.page_count_field, self.metadata_field, self.result_field) if name is not None]
        names.extend(self.configured_output_fields().values())
        return names

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.blob_ref_field})


_PROBE_DOCUMENT_BYTES = b"\x89PNG\r\n\x1a\n" + b"textract-inline-invariant-probe"
_PROBE_DOCUMENT_SHA256 = hashlib.sha256(_PROBE_DOCUMENT_BYTES).hexdigest()


class AWSTextractInlineAnalysis(BaseTransform, BatchTransformMixin):
    """Enrich managed-blob document rows through synchronous Amazon Textract analysis."""

    # blob_ref_field is an INPUT column; these four are "Optional output
    # field for ..." per their own config descriptions.
    output_naming_config_keys = frozenset({"text_field", "page_count_field", "metadata_field", "result_field"})
    name = "aws_textract_inline_analysis"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:7beadca2550f4b0f"
    config_model = AWSTextractInlineAnalysisConfig
    passes_through_input = True
    creates_tokens = False
    audit_characteristics = frozenset({AuditCharacteristic.CREDENTIALS})
    # The catalogue caps tags at six; "document" is carried by textract/ocr.
    capability_tags = ("aws", "textract", "ocr", "inline", "blob", "enrichment")

    usage_when_to_use = (
        "Use when each row carries a payload-store content hash (from the blob_rows source) for a JPEG, PNG, or "
        "single-page PDF document up to 5 MiB and you need synchronous Textract OCR, forms, tables, queries, "
        "signatures, or layout enrichment. Extracted remote content remains untrusted before LLM consumption."
    )
    usage_when_not_to_use = (
        "Not for multipage or larger documents, or documents already stored in S3 — use "
        "aws_textract_document_analysis for those. AnalyzeDocument has no idempotency guarantee, so SDK "
        "and engine retries can each repeat a billable provider call."
    )
    example_use = (
        "transform:\n"
        "  plugin: aws_textract_inline_analysis\n"
        "  options:\n"
        "    region: ap-southeast-2\n"
        "    auth_mode: default_chain\n"
        "    document_format: png\n"
        "    feature_types: [TABLES, FORMS]\n"
        "    text_field: textract_text\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "region": TEXTRACT_INVARIANT_PROBE_REGION,
            "auth_mode": "default_chain",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = AWSTextractInlineAnalysisConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._cfg = cfg

        self._region = cfg.region
        self._aws_access_key_id = cfg.aws_access_key_id
        self._aws_secret_access_key = cfg.aws_secret_access_key
        self._aws_session_token = cfg.aws_session_token
        self._blob_ref_field = cfg.blob_ref_field
        self._document_format: BinaryDocumentFormat = cfg.document_format
        self._feature_types = tuple(sorted(cfg.feature_types))
        self._query_requests: tuple[Mapping[str, Any], ...] = tuple(
            MappingProxyType(
                {
                    "text": query.text,
                    "alias": query.alias,
                    "pages": tuple(query.pages),
                }
            )
            for query in cfg.queries
        )
        self._text_field = cfg.text_field
        self._page_count_field = cfg.page_count_field
        self._metadata_field = cfg.metadata_field
        self._result_field = cfg.result_field
        self._facet_fields = cfg.configured_output_fields()
        self._max_document_bytes = cfg.max_document_bytes
        self._max_blocks = cfg.max_blocks
        self._max_result_bytes = cfg.max_result_bytes
        self._request_timeout_seconds = cfg.request_timeout_seconds
        # The effective wait must cover every botocore attempt (connect +
        # read) plus scheduling headroom, or the batch mixin would time a
        # row out while its final SDK attempt is still legitimately running.
        self._effective_batch_wait_timeout_seconds = max(
            float(cfg.batch_wait_timeout_seconds),
            (float(cfg.request_timeout_seconds) + _TEXTRACT_SDK_CONNECT_TIMEOUT_SECONDS) * SDK_TOTAL_MAX_ATTEMPTS
            + _TEXTRACT_SDK_TIMEOUT_HEADROOM_SECONDS,
        )

        self.declared_output_fields = frozenset(cfg.all_output_field_names())
        self._reject_input_options_naming_created_fields({"blob_ref_field": cfg.blob_ref_field})
        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "AWSTextractInlineAnalysis",
            adds_fields=True,
        )
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

        self._recorder: PluginAuditWriter | None = None
        self._run_id = ""
        self._node_id = ""
        self._telemetry_emit: Callable[[Any], None] = _warn_telemetry_before_start
        self._limiter: Any = None
        self._payload_store: PayloadStore | None = None
        self._sdk_client: TextractSyncSDKClient | None = None
        self._row_clients: dict[str, TextractInlineClient] = {}
        self._row_clients_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._batch_initialized = False

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        if ctx.landscape is None:
            raise FrameworkBugError("Amazon Textract inline transform requires Landscape audit recording")
        node_id = ctx.node_id if ctx.node_id is not None else self.node_id
        if node_id is None:
            raise FrameworkBugError("Amazon Textract inline transform requires a node_id before startup")
        self._recorder = ctx.landscape
        self._run_id = ctx.run_id
        self._node_id = node_id
        self._telemetry_emit = ctx.telemetry_emit
        self._limiter = ctx.rate_limit_registry.get_limiter(self.name) if ctx.rate_limit_registry is not None else None
        self._payload_store = ctx.payload_store
        if ctx.shutdown_event is not None:
            self._shutdown = ctx.shutdown_event
        if self._sdk_client is None:
            self._sdk_client = build_textract_sync_sdk_client(
                region=self._region,
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                aws_session_token=self._aws_session_token,
                read_timeout=self._request_timeout_seconds,
            )

    def connect_output(self, output: OutputPort, max_pending: int = 30) -> None:
        if self._batch_initialized:
            raise RuntimeError("connect_output() already called")
        self.init_batch_processing(
            max_pending=max_pending,
            output=output,
            name=self.name,
            max_workers=max_pending,
            batch_wait_timeout=self._effective_batch_wait_timeout_seconds,
        )
        self._batch_initialized = True

    def accept(self, row: PipelineRow, ctx: TransformContext) -> None:
        if not self._batch_initialized:
            raise RuntimeError("connect_output() must be called before accept().")
        self.accept_row(row, ctx, self._process_row)

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del row, ctx
        raise NotImplementedError(f"{self.__class__.__name__} uses row-level pipelining. Use accept() instead of process().")

    def close(self) -> None:
        self._shutdown.set()
        if self._batch_initialized:
            self.shutdown_batch_processing()
        with self._row_clients_lock:
            self._row_clients.clear()
        sdk_client = self._sdk_client
        self._sdk_client = None
        if sdk_client is not None:
            sdk_client.close()
        self._recorder = None
        self._limiter = None
        self._payload_store = None

    def _get_row_client(self, state_id: str, *, token_id: str | None) -> TextractInlineClient:
        with self._row_clients_lock:
            existing = self._row_clients.get(state_id)
            if existing is not None:
                return existing
            if self._recorder is None or self._sdk_client is None or not self._run_id:
                raise FrameworkBugError("Amazon Textract inline transform used before on_start")
            client = TextractInlineClient(
                execution=self._recorder,
                state_id=state_id,
                run_id=self._run_id,
                telemetry_emit=self._telemetry_emit,
                region=self._region,
                sdk_client=self._sdk_client,
                max_response_bytes=self._max_result_bytes,
                limiter=self._limiter,
                token_id=token_id,
            )
            self._row_clients[state_id] = client
            return client

    def _process_row(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        if ctx.state_id is None:
            raise FrameworkBugError("Amazon Textract inline batch processing requires state_id")
        if ctx.token is None:
            raise FrameworkBugError("Amazon Textract inline batch processing requires token identity")
        try:
            return self._process_single_with_state(row, ctx.state_id, token_id=ctx.token.token_id)
        finally:
            with self._row_clients_lock:
                self._row_clients.pop(ctx.state_id, None)

    def _read_document_bytes(self, row: PipelineRow) -> tuple[str, bytes] | TransformResult:
        """Resolve and locally validate the row's document bytes.

        Payload integrity corruption (``IntegrityError``) deliberately
        propagates: the payload store's content no longer matching its own
        hash is a Tier-1 infrastructure failure, not a row-data error.
        """
        field_name = self._blob_ref_field
        if field_name not in row:
            return TransformResult.error({"reason": "missing_field", "field": field_name}, retryable=False)
        payload_ref = row[field_name]
        if type(payload_ref) is not str:
            return TransformResult.error(
                {"reason": "invalid_input", "field": field_name, "actual_type": type(payload_ref).__name__},
                retryable=False,
            )
        if _PAYLOAD_REF_PATTERN.fullmatch(payload_ref) is None:
            return TransformResult.error(
                {"reason": "invalid_input", "field": field_name, "error_type": "invalid_payload_ref"},
                retryable=False,
            )
        store = self._payload_store
        if store is None:
            raise FrameworkBugError("Amazon Textract inline transform requires a payload store")
        try:
            content = store.retrieve(payload_ref)
        except PayloadNotFoundError:
            return TransformResult.error(
                {"reason": "blob_not_found", "field": field_name, "blob_ref": payload_ref},
                retryable=False,
            )
        if not content:
            return TransformResult.error(
                {"reason": "invalid_input", "field": field_name, "error_type": "empty_document"},
                retryable=False,
            )
        if len(content) > self._max_document_bytes:
            return TransformResult.error(
                {
                    "reason": "blob_too_large",
                    "field": field_name,
                    "max_blob_bytes": self._max_document_bytes,
                    "actual": str(len(content)),
                },
                retryable=False,
            )
        if not binary_document_signature_matches(self._document_format, content):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": field_name,
                    "error_type": "document_signature_mismatch",
                    "expected": self._document_format,
                },
                retryable=False,
            )
        return payload_ref, content

    def _process_single_with_state(self, row: PipelineRow, state_id: str, *, token_id: str | None) -> TransformResult:
        if not self._run_id or not self._node_id or token_id is None:
            raise FrameworkBugError("Amazon Textract inline processing requires run, node, and token identity")
        if self._shutdown.is_set():
            return TransformResult.error({"reason": "shutdown_requested"}, retryable=False)
        document = self._read_document_bytes(row)
        if isinstance(document, TransformResult):
            return document
        payload_ref, content = document

        client = self._get_row_client(state_id, token_id=token_id)
        try:
            analysis = client.analyze_document(
                document_bytes=content,
                document_sha256=payload_ref,
                document_format=self._document_format,
                feature_types=self._feature_types,
                queries=self._query_requests,
            )
        except TextractServiceError as error:
            return TransformResult.error(
                {"reason": "analysis_failed", "error_type": "service_error", "code": error.code},
                retryable=error.retryable,
            )
        except TextractResponseError as error:
            response_reason: TransformErrorCategory = "result_too_large" if error.category == "response_too_large" else "malformed_response"
            return TransformResult.error({"reason": response_reason, "error_type": "analyze_response"}, retryable=False)

        try:
            normalized = normalize_analyze_document_result(
                response=analysis.semantic_response,
                feature_types=self._feature_types,
                max_blocks=self._max_blocks,
                max_result_bytes=self._max_result_bytes,
            )
        except MalformedTextractResponse as error:
            safe_message = str(error)
            validation_reason: TransformErrorCategory
            if "max_blocks" in safe_message:
                validation_reason = "too_many_blocks"
            elif "max_result_bytes" in safe_message:
                validation_reason = "result_too_large"
            else:
                validation_reason = "malformed_response"
            return TransformResult.error({"reason": validation_reason, "error_type": "result_validation"}, retryable=False)

        if normalized.page_count != 1:
            return TransformResult.error(
                {
                    "reason": "validation_failed",
                    "error_type": "page_count",
                    "expected": "1",
                    "actual": str(normalized.page_count),
                },
                retryable=False,
            )

        return self._build_enriched_result(
            row,
            normalized,
            document_sha256=payload_ref,
            document_size_bytes=len(content),
            sdk_attempts=analysis.sdk_attempts,
        )

    def _build_enriched_result(
        self,
        row: PipelineRow,
        normalized: NormalizedTextractResult,
        *,
        document_sha256: str,
        document_size_bytes: int,
        sdk_attempts: int,
    ) -> TransformResult:
        output = row.to_dict()
        if self._text_field is not None:
            output[self._text_field] = normalized.text
        if self._page_count_field is not None:
            output[self._page_count_field] = normalized.page_count
        if self._metadata_field is not None:
            output[self._metadata_field] = {
                "document_sha256": document_sha256,
                "document_size_bytes": document_size_bytes,
                "document_format": self._document_format,
                **cast(dict[str, Any], deep_thaw(normalized.metadata)),
            }
        if self._result_field is not None:
            output[self._result_field] = deep_thaw(normalized.native_result)
        facet_values = {
            "pages": normalized.pages,
            "tables": normalized.tables,
            "forms": normalized.forms,
            "queries": normalized.queries,
            "signatures": normalized.signatures,
            "layout": normalized.layout,
        }
        for facet_name, field_name in self._facet_fields.items():
            output[field_name] = deep_thaw(facet_values[facet_name])

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output)
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)
        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "enriched",
                "fields_added": sorted(self.declared_output_fields),
                "metadata": {
                    "document_sha256": document_sha256,
                    "document_size_bytes": document_size_bytes,
                    "document_format": self._document_format,
                    "page_count": normalized.page_count,
                    "block_count": normalized.block_count,
                    "model_version": normalized.metadata["model_version"],
                    "feature_types": list(self._feature_types),
                    # AnalyzeDocument has no idempotency token: every SDK
                    # attempt may bill, so the attempt count is per-row
                    # billing evidence.
                    "sdk_attempts": sdk_attempts,
                    "result_status": "succeeded",
                },
            },
        )

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        return [self._augment_invariant_probe_row(probe, field_name=self._blob_ref_field, value=_PROBE_DOCUMENT_SHA256)]

    def execute_forward_invariant_probe(self, probe_rows: list[PipelineRow], ctx: Any) -> TransformResult:
        if len(probe_rows) != 1:
            raise FrameworkBugError(f"{self.__class__.__name__}.execute_forward_invariant_probe() requires exactly one row")
        if ctx.landscape is None:
            raise FrameworkBugError("Amazon Textract inline invariant probe requires Landscape audit recording")

        class _ProbeSDK:
            def analyze_document(self, **_kwargs: Any) -> Mapping[str, Any]:
                return {
                    "DocumentMetadata": {"Pages": 1},
                    "AnalyzeDocumentModelVersion": "probe-model",
                    "Blocks": [
                        {"BlockType": "PAGE", "Id": "page-probe", "Page": 1},
                        {
                            "BlockType": "LINE",
                            "Id": "line-probe",
                            "Page": 1,
                            "Text": "probe",
                            "Confidence": 100.0,
                        },
                    ],
                    "ResponseMetadata": {"RetryAttempts": 0},
                }

            def close(self) -> None:
                return None

        class _ProbePayloadStore:
            def store(self, content: bytes) -> str:
                raise FrameworkBugError("invariant probe must never store payloads")

            def retrieve(self, content_hash: str) -> bytes:
                if content_hash != _PROBE_DOCUMENT_SHA256:
                    raise PayloadNotFoundError(content_hash)
                return _PROBE_DOCUMENT_BYTES

            def exists(self, content_hash: str) -> bool:
                return content_hash == _PROBE_DOCUMENT_SHA256

            def delete(self, content_hash: str) -> bool:
                raise FrameworkBugError("invariant probe must never delete payloads")

        prior_recorder = self._recorder
        prior_run_id = self._run_id
        prior_node_id = self._node_id
        prior_telemetry = self._telemetry_emit
        prior_sdk = self._sdk_client
        prior_limiter = self._limiter
        prior_payload_store = self._payload_store
        prior_clients = self._row_clients
        prior_shutdown = self._shutdown
        try:
            self._recorder = ctx.landscape
            self._run_id = ctx.run_id
            self._node_id = "textract-inline-invariant-probe"
            self._telemetry_emit = ctx.telemetry_emit
            self._sdk_client = _ProbeSDK()
            self._limiter = None
            self._payload_store = _ProbePayloadStore()
            self._row_clients = {}
            self._shutdown = threading.Event()
            state_id = ctx.state_id or "textract-inline-invariant-probe-state"
            token_id = ctx.token.token_id if ctx.token is not None else "textract-inline-invariant-probe-token"
            return self._process_single_with_state(probe_rows[0], state_id, token_id=token_id)
        finally:
            self._recorder = prior_recorder
            self._run_id = prior_run_id
            self._node_id = prior_node_id
            self._telemetry_emit = prior_telemetry
            self._sdk_client = prior_sdk
            self._limiter = prior_limiter
            self._payload_store = prior_payload_store
            self._row_clients = prior_clients
            self._shutdown = prior_shutdown

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary=(
                "Analyze managed-blob documents synchronously with Amazon Textract: each row's payload hash is "
                "retrieved, signature-verified, and sent as inline bytes to one audited AnalyzeDocument call."
            ),
            composer_hints=(
                "Rows come from the blob_rows source; the default blob_ref_field matches its blob_ref output.",
                "document_format is required and never inferred — spell JPEG as jpeg; the runtime rejects a "
                "byte-signature mismatch fail-closed.",
                "PDF support is single-page only; mixed formats need homogeneous sources or branches with one "
                "transform instance per format.",
                "Multipage, oversized, or already-S3 documents belong in aws_textract_document_analysis.",
                "Synchronous AnalyzeDocument has no idempotency guarantee, so SDK and engine retries can each repeat a billable provider call.",
                "For explicit AWS credentials, use ELSPETH markers such as {secret_ref: AWS_ACCESS_KEY_ID}.",
            ),
        )
