"""Asynchronous Amazon Textract document-analysis transform."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import (
    BucketRegionVerificationEvidence,
    FrameworkBugError,
    TransformErrorCategory,
    TransformErrorReason,
)
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import canonical_json
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.batching import BatchTransformMixin, OutputPort
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.telemetry import make_warn_telemetry_before_start
from elspeth.plugins.transforms.aws.textract_bucket_region import (
    BucketRegionCoordinator,
    BucketRegionProof,
    BucketRegionUnverifiedError,
    BucketRegionVerification,
    HeadBucketClient,
    S3HeadBucketSDKClient,
    build_s3_head_bucket_sdk_client,
)
from elspeth.plugins.transforms.aws.textract_client import (
    StartAnalysisReceipt,
    TextractClient,
    TextractIdempotencyInvariantError,
    TextractResponseError,
    TextractSDKClient,
    TextractServiceError,
    build_textract_sdk_client,
)
from elspeth.plugins.transforms.aws.textract_regions import TEXTRACT_INVARIANT_PROBE_REGION, TEXTRACT_REGIONS
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    NormalizedTextractResult,
    normalize_textract_result,
)

FeatureType = Literal["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"]
AuthMode = Literal["default_chain", "secret_refs"]

_FEATURE_TYPES = frozenset({"TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"})
_QUERY_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9\s!\"#$%'&()*+,\-./:;=?@[\\\]^_`{|}~><]+$")
_QUERY_PAGE_PATTERN = re.compile(r"^[0-9*\-]+$")
_FACET_NAMES = ("pages", "tables", "forms", "queries", "signatures", "layout")
_BUCKET_PATTERN = re.compile(r"^[0-9A-Za-z.\-_]*$")
_SDK_TIMEOUT_HEADROOM_SECONDS = 90.0

# Textract raises InvalidS3ObjectException whenever it cannot READ the object —
# it does not distinguish authorization failures from missing or corrupt files.
# In scoped deployments the most common cause is an object outside the S3 read
# prefix granted to the runtime role, so classify it with an access-scope hint
# rather than letting operators suspect the document itself.
_S3_OBJECT_UNREADABLE_CODE = "InvalidS3ObjectException"
_S3_OBJECT_UNREADABLE_CAUSE = "s3_object_unreadable"
_S3_OBJECT_UNREADABLE_HINT = (
    "Amazon Textract could not read the S3 object. Most commonly the object is outside the "
    "S3 read scope granted to the pipeline's AWS role (check the role's s3:GetObject prefix "
    "against the document's bucket/key and, when version_field is configured, its "
    "s3:GetObjectVersion permission); it can also mean the object does not exist or is stored "
    "in a different region than the Textract endpoint. Verify access scope before suspecting "
    "a corrupt or unsupported file."
)

logger = structlog.get_logger(__name__)
_warn_telemetry_before_start = make_warn_telemetry_before_start(logger)


def _require_non_whitespace(value: str | None, *, field_name: str) -> str | None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    return value


class TextractQueryConfig(BaseModel):
    """One Textract query and its optional page selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=200)
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    pages: list[str] = Field(default_factory=list)

    @field_validator("text", "alias")
    @classmethod
    def _provider_text(cls, value: str | None) -> str | None:
        if value is not None and _QUERY_TEXT_PATTERN.fullmatch(value) is None:
            raise ValueError("value contains characters unsupported by Amazon Textract queries")
        return value

    @field_validator("pages")
    @classmethod
    def _page_selectors(cls, selectors: list[str]) -> list[str]:
        if len(set(selectors)) != len(selectors):
            raise ValueError("pages must not contain duplicate selectors")
        if "*" in selectors and selectors != ["*"]:
            raise ValueError("pages selector '*' must be the only selector")
        for selector in selectors:
            if not 1 <= len(selector) <= 9 or _QUERY_PAGE_PATTERN.fullmatch(selector) is None:
                raise ValueError("pages selectors must be 1-9 characters matching ^[0-9*-]+$")
            if selector == "*":
                continue
            parts = selector.split("-")
            if len(parts) == 1:
                if not parts[0].isdigit() or int(parts[0]) <= 0:
                    raise ValueError("pages selectors must use positive page numbers")
                continue
            if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) <= 0:
                raise ValueError("pages ranges must start with a positive page number")
            start = int(parts[0])
            end_text = parts[1]
            if end_text == "*":
                continue
            if not end_text.isdigit() or int(end_text) <= 0 or int(end_text) < start:
                raise ValueError("pages ranges must have positive, ordered endpoints")
        return selectors


class TextractExtractFields(BaseModel):
    """Optional normalized facet-to-row-field mappings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: str | None = None
    tables: str | None = None
    forms: str | None = None
    queries: str | None = None
    signatures: str | None = None
    layout: str | None = None


class AWSTextractDocumentAnalysisConfig(TransformDataConfig):
    """Validated public configuration for asynchronous S3 document analysis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        **TransformDataConfig.model_config,
        hide_input_in_errors=True,
    )

    region: str = Field(min_length=1, max_length=64, description="AWS region containing the S3 object and Textract endpoint.")
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

    bucket_field: str = Field(min_length=1, max_length=256, description="Input row field containing the S3 bucket name.")
    key_field: str = Field(min_length=1, max_length=256, description="Input row field containing the S3 object key.")
    version_field: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Optional input row field containing an S3 object version.",
    )
    feature_types: list[FeatureType] = Field(
        min_length=1,
        max_length=5,
        description="Unique Textract analysis features selected from TABLES, FORMS, QUERIES, SIGNATURES, and LAYOUT.",
    )
    queries: list[TextractQueryConfig] = Field(
        default_factory=list,
        max_length=30,
        description="Textract query definitions; configured together with the QUERIES feature.",
    )

    text_field: str | None = Field(default=None, description="Optional output field for page-ordered LINE text.")
    page_count_field: str | None = Field(default=None, description="Optional output field for the validated document page count.")
    metadata_field: str | None = Field(default=None, description="Optional output field for bounded Textract job metadata.")
    extract: TextractExtractFields = Field(
        default_factory=TextractExtractFields,
        description="Optional mappings from normalized document facets to output row fields.",
    )
    result_field: str | None = Field(default=None, description="Optional output field for the bounded provider-shaped aggregate.")

    poll_interval_seconds: float = Field(default=1.0, gt=0, description="Initial delay between non-terminal job-status polls.")
    poll_backoff_multiplier: float = Field(default=1.5, ge=1, description="Multiplier applied to successive job-status poll delays.")
    poll_max_interval_seconds: float = Field(default=10.0, gt=0, description="Maximum delay between job-status polls.")
    poll_timeout_seconds: float = Field(default=3600.0, gt=0, description="Total submit-through-terminal-status deadline in seconds.")
    batch_wait_timeout_seconds: float = Field(default=3900.0, gt=0, description="Maximum engine wait for one pipelined document row.")
    max_result_pages: int = Field(default=1000, gt=0, description="Maximum GetDocumentAnalysis result pages retained for one document.")
    max_blocks: int = Field(default=200_000, gt=0, description="Maximum combined Textract block count for one document.")
    max_result_bytes: int = Field(default=50_000_000, gt=0, description="Maximum canonical JSON bytes retained for one document result.")

    @field_validator(
        "poll_interval_seconds",
        "poll_backoff_multiplier",
        "poll_max_interval_seconds",
        "poll_timeout_seconds",
        "batch_wait_timeout_seconds",
    )
    @classmethod
    def _finite_timing(cls, value: float, info: object) -> float:
        if not math.isfinite(value):
            field_name = getattr(info, "field_name", "timing value")
            raise ValueError(f"{field_name} must be finite")
        return value

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        if value not in TEXTRACT_REGIONS:
            raise ValueError("region must be a supported Amazon Textract region")
        return value

    @field_validator("bucket_field", "key_field", "version_field", "text_field", "page_count_field", "metadata_field", "result_field")
    @classmethod
    def _field_name(cls, value: str | None, info: object) -> str | None:
        field_name = getattr(info, "field_name", "field")
        return _require_non_whitespace(value, field_name=field_name)

    @field_validator("feature_types")
    @classmethod
    def _features(cls, value: list[FeatureType]) -> list[FeatureType]:
        if any(feature not in _FEATURE_TYPES for feature in value):
            raise ValueError(f"feature_types must use only {sorted(_FEATURE_TYPES)}")
        if len(set(value)) != len(value):
            raise ValueError("feature_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        for facet_name in _FACET_NAMES:
            _require_non_whitespace(getattr(self.extract, facet_name), field_name=f"extract.{facet_name}")

        has_queries = bool(self.queries)
        has_query_feature = "QUERIES" in self.feature_types
        if has_queries != has_query_feature:
            raise ValueError("queries and the QUERIES feature must be configured together")

        access_present = self.aws_access_key_id is not None
        secret_present = self.aws_secret_access_key is not None
        session_present = self.aws_session_token is not None
        if self.auth_mode == "default_chain":
            if access_present or secret_present or session_present:
                raise ValueError("explicit credentials are forbidden in default_chain auth mode")
        elif access_present != secret_present:
            raise ValueError("aws_access_key_id and aws_secret_access_key are required together as a credential pair")
        elif not access_present:
            if session_present:
                raise ValueError("aws_session_token requires the access and secret credential pair")
            raise ValueError("aws_access_key_id and aws_secret_access_key are required together in secret_refs auth mode")

        if self.poll_max_interval_seconds < self.poll_interval_seconds:
            raise ValueError("poll_max_interval_seconds must be >= poll_interval_seconds")

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
        return {facet_name: field_name for facet_name in _FACET_NAMES if (field_name := getattr(self.extract, facet_name)) is not None}

    def all_output_field_names(self) -> list[str]:
        """Return every configured output row field in stable projection order."""
        names = [name for name in (self.text_field, self.page_count_field, self.metadata_field, self.result_field) if name is not None]
        names.extend(self.configured_output_fields().values())
        return names

    @property
    def declared_input_fields(self) -> frozenset[str]:
        fields = {self.bucket_field, self.key_field}
        if self.version_field is not None:
            fields.add(self.version_field)
        return super().declared_input_fields | frozenset(fields)


class AWSTextractDocumentAnalysis(BaseTransform, BatchTransformMixin):
    """Enrich S3 document references through asynchronous Amazon Textract analysis."""

    name = "aws_textract_document_analysis"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:cc0d5b8beb151b01"
    config_model = AWSTextractDocumentAnalysisConfig
    passes_through_input = True
    creates_tokens = False
    audit_characteristics = frozenset({AuditCharacteristic.CREDENTIALS})
    capability_tags = ("aws", "textract", "document", "ocr", "enrichment")
    web_config_authority = WebConfigAuthority.OPERATOR_PROFILED

    usage_when_to_use = (
        "Use when each row names a document already stored in S3 and you need asynchronous Textract "
        "OCR, forms, tables, queries, signatures, or layout enrichment. The runtime identity needs S3 read "
        "scope, and extracted remote content remains untrusted before LLM consumption."
    )
    usage_when_not_to_use = (
        "Not for inline bytes, document URLs, or synchronous low-latency analysis. This catalogue has no "
        "registered synchronous Textract transform; stage each document in S3 before using this plugin."
    )
    example_use = (
        "transform:\n"
        "  plugin: aws_textract_document_analysis\n"
        "  options:\n"
        "    region: ap-southeast-2\n"
        "    auth_mode: default_chain\n"
        "    bucket_field: document_bucket\n"
        "    key_field: document_key\n"
        "    feature_types: [TABLES, FORMS]\n"
        "    text_field: textract_text\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "region": TEXTRACT_INVARIANT_PROBE_REGION,
            "auth_mode": "default_chain",
            "bucket_field": "document_bucket",
            "key_field": "document_key",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = AWSTextractDocumentAnalysisConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._cfg = cfg

        self._region = cfg.region
        self._aws_access_key_id = cfg.aws_access_key_id
        self._aws_secret_access_key = cfg.aws_secret_access_key
        self._aws_session_token = cfg.aws_session_token
        self._bucket_field = cfg.bucket_field
        self._key_field = cfg.key_field
        self._version_field = cfg.version_field
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
        self._poll_interval_seconds = cfg.poll_interval_seconds
        self._poll_backoff_multiplier = cfg.poll_backoff_multiplier
        self._poll_max_interval_seconds = cfg.poll_max_interval_seconds
        self._poll_timeout_seconds = cfg.poll_timeout_seconds
        self._max_result_pages = cfg.max_result_pages
        self._max_blocks = cfg.max_blocks
        self._max_result_bytes = cfg.max_result_bytes
        self._effective_batch_wait_timeout_seconds = max(
            float(cfg.batch_wait_timeout_seconds),
            float(cfg.poll_timeout_seconds) + _SDK_TIMEOUT_HEADROOM_SECONDS,
        )

        self.declared_output_fields = frozenset(cfg.all_output_field_names())
        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "AWSTextractDocumentAnalysis",
            adds_fields=True,
        )
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

        self._recorder: PluginAuditWriter | None = None
        self._run_id = ""
        self._node_id = ""
        self._telemetry_emit: Callable[[Any], None] = _warn_telemetry_before_start
        self._limiter: Any = None
        self._sdk_client: TextractSDKClient | None = None
        self._s3_sdk_client: S3HeadBucketSDKClient | None = None
        self._bucket_region_coordinator = BucketRegionCoordinator()
        self._row_clients: dict[str, TextractClient] = {}
        self._row_clients_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._batch_initialized = False

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        if ctx.landscape is None:
            raise FrameworkBugError("Amazon Textract transform requires Landscape audit recording")
        node_id = ctx.node_id if ctx.node_id is not None else self.node_id
        if node_id is None:
            raise FrameworkBugError("Amazon Textract transform requires a node_id before startup")
        self._recorder = ctx.landscape
        self._run_id = ctx.run_id
        self._node_id = node_id
        self._telemetry_emit = ctx.telemetry_emit
        self._limiter = ctx.rate_limit_registry.get_limiter(self.name) if ctx.rate_limit_registry is not None else None
        if ctx.shutdown_event is not None:
            self._shutdown = ctx.shutdown_event
        self._bucket_region_coordinator = BucketRegionCoordinator()
        created_s3 = False
        created_textract = False
        try:
            if self._s3_sdk_client is None:
                self._s3_sdk_client = build_s3_head_bucket_sdk_client(
                    region=self._region,
                    aws_access_key_id=self._aws_access_key_id,
                    aws_secret_access_key=self._aws_secret_access_key,
                    aws_session_token=self._aws_session_token,
                )
                created_s3 = True
            if self._sdk_client is None:
                self._sdk_client = build_textract_sdk_client(
                    region=self._region,
                    aws_access_key_id=self._aws_access_key_id,
                    aws_secret_access_key=self._aws_secret_access_key,
                    aws_session_token=self._aws_session_token,
                )
                created_textract = True
        except Exception:
            if created_textract and self._sdk_client is not None:
                self._sdk_client.close()
                self._sdk_client = None
            if created_s3 and self._s3_sdk_client is not None:
                self._s3_sdk_client.close()
                self._s3_sdk_client = None
            raise

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
        self._bucket_region_coordinator = BucketRegionCoordinator()
        with self._row_clients_lock:
            self._row_clients.clear()
        sdk_client = self._sdk_client
        self._sdk_client = None
        if sdk_client is not None:
            sdk_client.close()
        s3_sdk_client = self._s3_sdk_client
        self._s3_sdk_client = None
        if s3_sdk_client is not None:
            s3_sdk_client.close()
        self._recorder = None
        self._limiter = None

    def _get_row_client(self, state_id: str, *, token_id: str | None) -> TextractClient:
        with self._row_clients_lock:
            existing = self._row_clients.get(state_id)
            if existing is not None:
                return existing
            if self._recorder is None or self._sdk_client is None or not self._run_id:
                raise FrameworkBugError("Amazon Textract transform used before on_start")
            client = TextractClient(
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
            raise FrameworkBugError("Amazon Textract batch processing requires state_id")
        if ctx.token is None:
            raise FrameworkBugError("Amazon Textract batch processing requires token identity")
        try:
            return self._process_single_with_state(row, ctx.state_id, token_id=ctx.token.token_id)
        finally:
            with self._row_clients_lock:
                self._row_clients.pop(ctx.state_id, None)

    def _read_document_location(self, row: PipelineRow) -> tuple[str, str, str | None] | TransformResult:
        required_fields = (self._bucket_field, self._key_field)
        for field_name in required_fields:
            if field_name not in row:
                return TransformResult.error({"reason": "missing_field", "field": field_name}, retryable=False)
        if self._version_field is not None and self._version_field not in row:
            return TransformResult.error({"reason": "missing_field", "field": self._version_field}, retryable=False)

        bucket = row[self._bucket_field]
        key = row[self._key_field]
        version = row[self._version_field] if self._version_field is not None else None
        for field_name, value in ((self._bucket_field, bucket), (self._key_field, key)):
            if type(value) is not str:
                return TransformResult.error(
                    {"reason": "invalid_input", "field": field_name, "actual_type": type(value).__name__},
                    retryable=False,
                )
        if self._version_field is not None and type(version) is not str:
            return TransformResult.error(
                {"reason": "invalid_input", "field": self._version_field, "actual_type": type(version).__name__},
                retryable=False,
            )
        assert isinstance(bucket, str)
        assert isinstance(key, str)
        assert version is None or isinstance(version, str)
        if not 3 <= len(bucket) <= 255 or _BUCKET_PATTERN.fullmatch(bucket) is None:
            return TransformResult.error(
                {"reason": "invalid_input", "field": self._bucket_field, "error_type": "invalid_s3_bucket"},
                retryable=False,
            )
        if not 1 <= len(key) <= 1024 or not key.strip() or "#" in key:
            return TransformResult.error(
                {"reason": "invalid_input", "field": self._key_field, "error_type": "invalid_s3_key"},
                retryable=False,
            )
        if version is not None and (not 1 <= len(version) <= 1024 or not version.strip()):
            assert self._version_field is not None
            return TransformResult.error(
                {"reason": "invalid_input", "field": self._version_field, "error_type": "invalid_s3_version"},
                retryable=False,
            )
        return bucket, key, version

    def _client_request_token(
        self,
        *,
        run_id: str,
        node_id: str,
        token_id: str,
        bucket: str,
        key: str,
        version: str | None,
    ) -> str:
        query_identity = canonical_json([dict(query) for query in self._query_requests])
        fields = (
            "elspeth:aws_textract_document_analysis:start",
            self.plugin_version,
            run_id,
            node_id,
            token_id,
            self._region,
            bucket,
            key,
            "0" if version is None else "1",
            "" if version is None else version,
            *self._feature_types,
            query_identity,
        )
        digest = hashlib.sha256()
        for field in fields:
            encoded = field.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _process_single_with_state(self, row: PipelineRow, state_id: str, *, token_id: str | None) -> TransformResult:
        if not self._run_id or not self._node_id or token_id is None:
            raise FrameworkBugError("Amazon Textract processing requires run, node, and token identity")
        if self._shutdown.is_set():
            return TransformResult.error({"reason": "shutdown_requested"}, retryable=False)
        location = self._read_document_location(row)
        if isinstance(location, TransformResult):
            return location
        bucket, key, version = location
        try:
            verification = self._bucket_region_coordinator.verify(
                bucket,
                lambda: self._verify_bucket_region_live(bucket, state_id=state_id, token_id=token_id),
            )
        except BucketRegionUnverifiedError as error:
            return TransformResult.error(
                {"reason": "bucket_region_unverified", "error_type": error.code},
                retryable=error.retryable,
            )
        result = self._process_verified_document(
            row,
            state_id=state_id,
            token_id=token_id,
            bucket=bucket,
            key=key,
            version=version,
            verification=verification,
        )
        return self._with_bucket_region_verification(result, verification)

    def _process_verified_document(
        self,
        row: PipelineRow,
        *,
        state_id: str,
        token_id: str,
        bucket: str,
        key: str,
        version: str | None,
        verification: BucketRegionVerification,
    ) -> TransformResult:
        if verification.region != self._region:
            return TransformResult.error(
                {
                    "reason": "bucket_region_mismatch",
                    "configured_region": self._region,
                    "observed_region": verification.region,
                },
                retryable=False,
            )
        started_at = time.monotonic()
        client = self._get_row_client(state_id, token_id=token_id)
        request_token = self._client_request_token(
            run_id=self._run_id,
            node_id=self._node_id,
            token_id=token_id,
            bucket=bucket,
            key=key,
            version=version,
        )
        try:
            receipt = client.start_document_analysis(
                bucket=bucket,
                key=key,
                version=version,
                feature_types=self._feature_types,
                queries=self._query_requests,
                client_request_token=request_token,
            )
        except TextractIdempotencyInvariantError as error:
            raise FrameworkBugError("Amazon Textract idempotency invariant failed") from error
        except TextractServiceError as error:
            submit_error: TransformErrorReason = {"reason": "submit_failed", "error_type": "service_error", "code": error.code}
            if error.code == _S3_OBJECT_UNREADABLE_CODE:
                submit_error["cause"] = _S3_OBJECT_UNREADABLE_CAUSE
                submit_error["error"] = _S3_OBJECT_UNREADABLE_HINT
            return TransformResult.error(submit_error, retryable=error.retryable)
        except TextractResponseError as error:
            submit_reason: TransformErrorCategory = "result_too_large" if error.category == "response_too_large" else "malformed_response"
            return TransformResult.error({"reason": submit_reason, "error_type": "submit_response"}, retryable=False)

        collected = self._poll_and_collect(client, receipt, started_at=started_at)
        if isinstance(collected, TransformResult):
            return collected
        result_pages = collected
        try:
            normalized = normalize_textract_result(
                job_id=receipt.job_id,
                result_pages=result_pages,
                feature_types=self._feature_types,
                s3_version=version,
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
        return self._build_enriched_result(row, receipt, normalized)

    def _bucket_region_verification_evidence(
        self,
        verification: BucketRegionVerification,
    ) -> BucketRegionVerificationEvidence:
        evidence = BucketRegionVerificationEvidence(
            configured_region=self._region,
            observed_region=verification.region,
            proof_source=verification.source,
            http_status=verification.http_status,
            cache_status=verification.cache_status,
        )
        if verification.provider_code is not None:
            evidence["provider_code"] = verification.provider_code
        return evidence

    def _with_bucket_region_verification(
        self,
        result: TransformResult,
        verification: BucketRegionVerification,
    ) -> TransformResult:
        evidence = self._bucket_region_verification_evidence(verification)
        if result.status == "error":
            assert result.reason is not None
            result.reason["bucket_region_verification"] = evidence
            return result
        assert result.success_reason is not None
        metadata = result.success_reason.setdefault("metadata", {})
        metadata["bucket_region_verification"] = evidence
        return result

    def _verify_bucket_region_live(self, bucket: str, *, state_id: str, token_id: str) -> BucketRegionProof:
        if self._recorder is None or self._s3_sdk_client is None or not self._run_id:
            raise FrameworkBugError("Amazon Textract bucket verification used before on_start")
        return HeadBucketClient(
            execution=self._recorder,
            state_id=state_id,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            region=self._region,
            sdk_client=self._s3_sdk_client,
            token_id=token_id,
        ).verify_bucket_region(bucket)

    def _poll_and_collect(
        self,
        client: TextractClient,
        receipt: StartAnalysisReceipt,
        *,
        started_at: float,
    ) -> tuple[Mapping[str, Any], ...] | TransformResult:
        deadline = started_at + self._poll_timeout_seconds
        interval = self._poll_interval_seconds
        next_token: str | None = None
        seen_tokens: set[str] = set()
        pages: list[Mapping[str, Any]] = []
        block_count = 0
        retained_result_bytes = 0
        while True:
            if self._shutdown.is_set():
                return TransformResult.error({"reason": "shutdown_requested"}, retryable=False)
            if time.monotonic() >= deadline:
                return TransformResult.error({"reason": "poll_timeout"}, retryable=True)
            try:
                page = client.get_document_analysis(job_id=receipt.job_id, next_token=next_token)
            except TextractIdempotencyInvariantError as error:
                raise FrameworkBugError("Amazon Textract idempotency invariant failed") from error
            except TextractServiceError as error:
                return TransformResult.error(
                    {"reason": "poll_failed", "error_type": "service_error", "code": error.code},
                    retryable=error.retryable,
                )
            except TextractResponseError as error:
                poll_reason: TransformErrorCategory = "result_too_large" if error.category == "response_too_large" else "malformed_response"
                return TransformResult.error({"reason": poll_reason, "error_type": "poll_response"}, retryable=False)

            if self._shutdown.is_set():
                return TransformResult.error({"reason": "shutdown_requested"}, retryable=False)
            if time.monotonic() >= deadline:
                return TransformResult.error({"reason": "poll_timeout"}, retryable=True)

            status = page.semantic_response.get("JobStatus")
            if type(status) is not str:
                return TransformResult.error({"reason": "malformed_response", "error_type": "job_status"}, retryable=False)
            if status == "IN_PROGRESS":
                if page.next_token is not None:
                    return TransformResult.error({"reason": "malformed_response", "error_type": "premature_next_token"}, retryable=False)
                now = time.monotonic()
                if now >= deadline:
                    return TransformResult.error({"reason": "poll_timeout"}, retryable=True)
                sleep_seconds = min(interval, deadline - now)
                if sleep_seconds > 0 and self._shutdown.wait(timeout=sleep_seconds):
                    return TransformResult.error({"reason": "shutdown_requested"}, retryable=False)
                interval = min(interval * self._poll_backoff_multiplier, self._poll_max_interval_seconds)
                continue
            if status == "FAILED":
                return TransformResult.error({"reason": "analysis_failed"}, retryable=False)
            if status == "PARTIAL_SUCCESS":
                return TransformResult.error({"reason": "partial_success"}, retryable=False)
            if status != "SUCCEEDED":
                return TransformResult.error({"reason": "malformed_response", "error_type": "unknown_status"}, retryable=False)

            try:
                page_bytes = len(canonical_json(page.semantic_response).encode("utf-8"))
            except (TypeError, ValueError, RecursionError, UnicodeError):
                return TransformResult.error({"reason": "malformed_response", "error_type": "result_encoding"}, retryable=False)
            retained_result_bytes += page_bytes
            if retained_result_bytes > self._max_result_bytes:
                return TransformResult.error({"reason": "result_too_large"}, retryable=False)
            pages.append(page.semantic_response)
            raw_blocks = page.semantic_response.get("Blocks")
            if isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes, bytearray)):
                block_count += len(raw_blocks)
                if block_count > self._max_blocks:
                    return TransformResult.error({"reason": "too_many_blocks"}, retryable=False)
            returned_token = page.next_token
            if returned_token is None:
                return tuple(pages)
            if returned_token in seen_tokens:
                return TransformResult.error({"reason": "pagination_cycle"}, retryable=False)
            seen_tokens.add(returned_token)
            if len(pages) >= self._max_result_pages:
                return TransformResult.error({"reason": "pagination_limit_exceeded"}, retryable=False)
            next_token = returned_token

    def _build_enriched_result(
        self,
        row: PipelineRow,
        receipt: StartAnalysisReceipt,
        normalized: NormalizedTextractResult,
    ) -> TransformResult:
        output = row.to_dict()
        if self._text_field is not None:
            output[self._text_field] = normalized.text
        if self._page_count_field is not None:
            output[self._page_count_field] = normalized.page_count
        if self._metadata_field is not None:
            output[self._metadata_field] = deep_thaw(normalized.metadata)
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
        metadata = normalized.metadata
        warnings = metadata["warnings"]
        warning_count = len(warnings) if isinstance(warnings, list | tuple) else 0
        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "enriched",
                "fields_added": sorted(self.declared_output_fields),
                "metadata": {
                    "job_id": receipt.job_id,
                    "page_count": normalized.page_count,
                    "block_count": normalized.block_count,
                    "model_version": metadata["model_version"],
                    "warning_count": warning_count,
                    "feature_types": list(self._feature_types),
                    "result_status": "succeeded",
                },
            },
        )

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        row = self._augment_invariant_probe_row(probe, field_name=self._bucket_field, value="probe-bucket")
        row = self._augment_invariant_probe_row(row, field_name=self._key_field, value="probe-document.pdf")
        return [row]

    def execute_forward_invariant_probe(self, probe_rows: list[PipelineRow], ctx: Any) -> TransformResult:
        if len(probe_rows) != 1:
            raise FrameworkBugError(f"{self.__class__.__name__}.execute_forward_invariant_probe() requires exactly one row")
        if ctx.landscape is None:
            raise FrameworkBugError("Amazon Textract invariant probe requires Landscape audit recording")

        class _ProbeSDK:
            def start_document_analysis(self, **_kwargs: Any) -> Mapping[str, Any]:
                return {"JobId": "job-probe", "ResponseMetadata": {"RetryAttempts": 0}}

            def get_document_analysis(self, **_kwargs: Any) -> Mapping[str, Any]:
                return {
                    "JobStatus": "SUCCEEDED",
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

        class _ProbeS3SDK:
            def head_bucket(self, **_kwargs: Any) -> Mapping[str, Any]:
                return {
                    "BucketRegion": self_region,
                    "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0},
                }

            def close(self) -> None:
                return None

        self_region = self._region

        prior_recorder = self._recorder
        prior_run_id = self._run_id
        prior_node_id = self._node_id
        prior_telemetry = self._telemetry_emit
        prior_sdk = self._sdk_client
        prior_s3_sdk = self._s3_sdk_client
        prior_bucket_region_coordinator = self._bucket_region_coordinator
        prior_limiter = self._limiter
        prior_clients = self._row_clients
        prior_shutdown = self._shutdown
        try:
            self._recorder = ctx.landscape
            self._run_id = ctx.run_id
            self._node_id = "textract-invariant-probe"
            self._telemetry_emit = ctx.telemetry_emit
            self._sdk_client = _ProbeSDK()
            self._s3_sdk_client = _ProbeS3SDK()
            self._bucket_region_coordinator = BucketRegionCoordinator()
            self._limiter = None
            self._row_clients = {}
            self._shutdown = threading.Event()
            state_id = ctx.state_id or "textract-invariant-probe-state"
            token_id = ctx.token.token_id if ctx.token is not None else "textract-invariant-probe-token"
            return self._process_single_with_state(probe_rows[0], state_id, token_id=token_id)
        finally:
            self._recorder = prior_recorder
            self._run_id = prior_run_id
            self._node_id = prior_node_id
            self._telemetry_emit = prior_telemetry
            self._sdk_client = prior_sdk
            self._s3_sdk_client = prior_s3_sdk
            self._bucket_region_coordinator = prior_bucket_region_coordinator
            self._limiter = prior_limiter
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
                "Analyze S3-backed documents asynchronously with Amazon Textract and enrich each row only after "
                "all result pages validate. Configure AWS identity through the default chain or ELSPETH secret refs."
            ),
            composer_hints=(
                "Provide bucket_field and key_field, choose one or more Textract feature_types, and map at least one output.",
                "QUERIES requires query definitions; inline document bytes belong in the separate synchronous plugin.",
                "For explicit AWS credentials, use ELSPETH markers such as {secret_ref: AWS_ACCESS_KEY_ID}.",
            ),
        )
