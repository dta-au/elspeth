"""Validated, bounded primitives for the optional AWS S3 sink plugin."""

from __future__ import annotations

import base64
import binascii
import codecs
import csv
import hashlib
import json
import math
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, BinaryIO, ClassVar, Literal, Never, Protocol, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from elspeth.contracts import ArtifactDescriptor, CallType, Determinism, PluginSchema
from elspeth.contracts.contexts import SinkContext
from elspeth.contracts.diversion import SinkWriteResult
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.header_modes import HeaderMode, parse_header_mode
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    ResolvedSinkEffectMode,
    RestagingSinkEffectCapability,
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
)
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.contracts.wire_visible_identity import reject_operator_required_placeholder_value
from elspeth.plugins.aws_s3_common import build_s3_client
from elspeth.plugins.infrastructure.base import BaseSink
from elspeth.plugins.infrastructure.config_base import (
    DataPluginConfig,
    HeaderModeOption,
    validate_headers_value,
)
from elspeth.plugins.infrastructure.display_headers import (
    apply_display_headers,
    get_effective_display_headers,
    init_display_headers,
    set_resume_field_resolution,
)
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sinks._diversion_attribution import DiversionAttribution, build_diversion_attribution
from elspeth.plugins.sinks._remote_object_effects import (
    RemoteObjectCollisionError,
    RemoteObjectObservation,
    RemoteObjectPreconditionError,
    inspect_remote_object,
    prepare_remote_object,
    reconcile_remote_observation,
    remote_commit_result,
    remote_stage_missing,
    require_commit_authority,
    restage_remote_object,
    validate_remote_plan,
)

_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_WRITE_CHUNK_BYTES = 64 * 1024
_MAX_BUCKET_CHARS = 2048
_MAX_KEY_TEMPLATE_BYTES = 4096
_MAX_RENDERED_KEY_BYTES = 1024
_MAX_ENDPOINT_CHARS = 2048
_MAX_REGION_CHARS = 64
_MAX_ETAG_BYTES = 1024
_SAFE_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CONDITIONAL_ERROR_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})
_DEFINITE_REJECTION_CODES = frozenset({"AccessDenied", "NoSuchBucket", "InvalidRequest"})


class CSVWriteOptions(BaseModel):
    """CSV output controls for the S3 sink."""

    model_config = {"extra": "forbid", "frozen": True}

    delimiter: str = Field(default=",", description="Single-character CSV field delimiter")
    encoding: str = Field(default="utf-8", description="Character encoding for CSV output")
    include_header: bool = Field(default=True, description="Write the CSV header record")

    @field_validator("delimiter")
    @classmethod
    def _validate_delimiter(cls, value: str) -> str:
        if len(value) != 1:
            raise ValueError("delimiter must be a single character")
        return value

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError("unknown CSV encoding") from exc
        probe_failed = False
        encoded: object = None
        try:
            encoder = codecs.getincrementalencoder(value)(errors="strict")
            encoded = encoder.encode("", final=True)
        except (LookupError, TypeError, UnicodeError, ValueError):
            probe_failed = True
        if probe_failed or not isinstance(encoded, bytes):
            raise ValueError("CSV encoding must encode text to bytes") from None
        return value


class AWSS3SinkConfig(DataPluginConfig):
    """Strict S3 sink configuration with no credential or raw client surface."""

    _plugin_component_type: ClassVar[str | None] = "sink"

    bucket: str = Field(..., description="S3 bucket name or access-point identifier")
    key: str = Field(..., description="S3 object key template rendered once per run")
    format: Literal["csv", "json", "jsonl"] = Field(default="csv", description="S3 object data format")
    overwrite: bool = Field(default=True, description="Allow replacement of an existing S3 object")
    csv_options: CSVWriteOptions = Field(default_factory=CSVWriteOptions, description="CSV writing options")
    headers: HeaderModeOption = Field(
        default=None,
        description="Normalized, original, or custom output headers",
    )
    region_name: str | None = Field(default=None, description="AWS signing region override")
    endpoint_url: str | None = Field(default=None, description="CLI/batch-only S3-compatible HTTP endpoint")
    max_object_bytes: int = Field(
        default=256 * 1024 * 1024,
        gt=0,
        le=1024 * 1024 * 1024,
        strict=True,
        description="Maximum serialized S3 object bytes",
    )
    max_record_chars: int = Field(
        default=1_000_000,
        gt=0,
        le=8_000_000,
        strict=True,
        description="Maximum characters in one serialized output record",
    )

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        if not value.strip() or len(value) > _MAX_BUCKET_CHARS:
            raise ValueError("bucket must be nonblank and at most 2048 characters")
        return reject_operator_required_placeholder_value(value, field_name="bucket")

    @field_validator("key")
    @classmethod
    def _validate_key_template(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > _MAX_KEY_TEMPLATE_BYTES:
            raise ValueError("key template must be nonblank and at most 4096 UTF-8 bytes")
        if _has_control_character(value):
            raise ValueError("key template must not contain control characters")
        reject_operator_required_placeholder_value(value, field_name="key")
        _compile_key_template(value)
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: str | dict[str, str] | None) -> str | dict[str, str] | None:
        return validate_headers_value(value)

    @field_validator("region_name")
    @classmethod
    def _validate_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or len(value) > _MAX_REGION_CHARS or re.fullmatch(r"[A-Za-z0-9-]+", value) is None:
            raise ValueError("region_name must be a bounded AWS region identifier")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or len(value) > _MAX_ENDPOINT_CHARS:
            raise ValueError("endpoint_url must be a bounded HTTP(S) URL")
        if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("endpoint_url must not contain whitespace or control characters")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint_url must be a valid HTTP(S) URL") from exc
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("endpoint_url must be an HTTP(S) URL with a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint_url must not contain a query or fragment")
        return value

    @model_validator(mode="after")
    def _validate_csv_only_options(self) -> Self:
        if self.format != "csv" and self.csv_options != CSVWriteOptions():
            raise ValueError("csv_options is only supported for CSV format")
        return self

    @property
    def headers_mode(self) -> HeaderMode:
        return parse_header_mode(self.headers) if self.headers is not None else HeaderMode.NORMALIZED

    @property
    def headers_mapping(self) -> dict[str, str] | None:
        return cast("dict[str, str]", self.headers) if self.headers_mode is HeaderMode.CUSTOM else None


AWSS3SinkConfig.model_rebuild()


class _CompiledKeyTemplate(Protocol):
    def render(self, **values: str) -> str: ...


class _S3Client(Protocol):
    def head_object(self, **kwargs: object) -> object: ...

    def put_object(self, **kwargs: object) -> object: ...

    def close(self) -> None: ...


class _S3ClientError(Protocol):
    response: object


def _load_jinja() -> tuple[type[Any], type[Exception], type[Any]]:
    """Load Jinja lazily so the base install can still discover other plugins."""
    try:
        from jinja2 import StrictUndefined, TemplateSyntaxError
        from jinja2.sandbox import SandboxedEnvironment
    except ImportError as exc:
        raise ImportError('Jinja2 is required for aws_s3 sinks; install Elspeth with the "aws" extra') from exc
    return StrictUndefined, TemplateSyntaxError, SandboxedEnvironment


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _compile_key_template(template_source: str) -> _CompiledKeyTemplate:
    """Compile the deliberately small key-template language without evaluating expressions."""
    strict_undefined, template_syntax_error, environment_type = _load_jinja()
    if not template_source.strip() or len(template_source.encode("utf-8")) > _MAX_KEY_TEMPLATE_BYTES:
        raise ValueError("key template must be nonblank and at most 4096 UTF-8 bytes")
    if _has_control_character(template_source):
        raise ValueError("key template must not contain control characters")
    environment = environment_type(undefined=strict_undefined)
    try:
        parsed = environment.parse(template_source)
    except template_syntax_error as exc:
        raise ValueError("key template syntax is invalid") from exc

    from jinja2 import nodes

    for body_node in parsed.body:
        if not isinstance(body_node, nodes.Output):
            raise ValueError("key template may contain only literal text and approved variables")
        for output_node in body_node.nodes:
            if isinstance(output_node, nodes.TemplateData):
                continue
            if isinstance(output_node, nodes.Name) and output_node.ctx == "load" and output_node.name in {"run_id", "timestamp"}:
                continue
            raise ValueError("key template may contain only literal text and approved variables")
    return cast("_CompiledKeyTemplate", environment.from_string(template_source))


def _validate_rendered_key(value: str) -> str:
    if not value.strip() or len(value.encode("utf-8")) > _MAX_RENDERED_KEY_BYTES:
        raise ValueError("rendered key must be nonblank and at most 1024 UTF-8 bytes")
    if _has_control_character(value):
        raise ValueError("rendered key must not contain control characters")
    return value


def _render_key_template(template_source: str, *, run_id: str, timestamp: str) -> str:
    template = _compile_key_template(template_source)
    rendered = template.render(run_id=run_id, timestamp=timestamp)
    return _validate_rendered_key(rendered)


class S3ObjectSizeLimitError(RuntimeError):
    """The complete candidate object exceeded its configured byte cap."""

    def __init__(self, observed_bytes: int, limit_bytes: int) -> None:
        super().__init__(f"S3 object exceeds configured byte limit ({observed_bytes} > {limit_bytes}).")
        self.observed_bytes = observed_bytes
        self.limit_bytes = limit_bytes


class S3RecordSerializationError(ValueError):
    """A row cannot be represented without exposing its value in the error."""

    def __init__(self) -> None:
        super().__init__("S3 output record could not be serialized safely.")


class S3RecordSizeLimitError(S3RecordSerializationError):
    """A row exceeded the configured character budget."""

    def __init__(self) -> None:
        ValueError.__init__(self, "S3 output record exceeds configured character limit.")


@dataclass(slots=True)
class _SerializedObject:
    """Own a rewound temporary object and its stable integrity metadata."""

    body: BinaryIO
    size_bytes: int
    content_hash: str
    checksum_sha256_b64: str
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.body.close()

    def __enter__(self) -> _SerializedObject:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _BoundedBinaryWriter:
    def __init__(self, body: BinaryIO, max_object_bytes: int) -> None:
        self._body = body
        self._max_object_bytes = max_object_bytes
        self.size_bytes = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        for offset in range(0, len(view), _WRITE_CHUNK_BYTES):
            chunk = view[offset : offset + _WRITE_CHUNK_BYTES]
            next_size = self.size_bytes + len(chunk)
            if next_size > self._max_object_bytes:
                raise S3ObjectSizeLimitError(next_size, self._max_object_bytes)
            self.digest.update(chunk)
            written = self._body.write(chunk)
            if written != len(chunk):
                raise OSError("temporary S3 object spool accepted a partial write")
            self.size_bytes = next_size


class _EncodedTextWriter:
    def __init__(self, writer: _BoundedBinaryWriter, encoding: str) -> None:
        self._writer = writer
        encoder_failed = False
        encoder: Any | None = None
        try:
            encoder = codecs.getincrementalencoder(encoding)(errors="strict")
        except (LookupError, TypeError, UnicodeError, ValueError):
            encoder_failed = True
        if encoder_failed or encoder is None:
            raise S3RecordSerializationError from None
        self._encoder = encoder

    def write(self, value: str) -> int:
        encoding_failed = False
        encoded: object = None
        try:
            encoded = self._encoder.encode(value, final=False)
        except (LookupError, TypeError, UnicodeError, ValueError):
            encoding_failed = True
        if encoding_failed or not isinstance(encoded, bytes):
            raise S3RecordSerializationError from None
        self._writer.write(encoded)
        return len(value)

    def finalize(self) -> None:
        encoding_failed = False
        encoded: object = None
        try:
            encoded = self._encoder.encode("", final=True)
        except (LookupError, TypeError, UnicodeError, ValueError):
            encoding_failed = True
        if encoding_failed or not isinstance(encoded, bytes):
            raise S3RecordSerializationError from None
        self._writer.write(encoded)


def _json_string_chars(value: str) -> int:
    total = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or character in {"\b", "\f", "\n", "\r", "\t"}:
            total += 2
        elif codepoint < 0x20:
            total += 6
        else:
            total += 1
    return total


def _json_value_chars(value: Any, *, seen: set[int]) -> int:
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, str):
        return _json_string_chars(value)
    if type(value) is int:
        return len(str(value))
    if type(value) is float:
        if not math.isfinite(value):
            raise S3RecordSerializationError
        return len(json.dumps(value, allow_nan=False))
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise S3RecordSerializationError
        seen.add(identity)
        try:
            total = 2
            for index, (key, child) in enumerate(value.items()):
                if not isinstance(key, str):
                    raise S3RecordSerializationError
                if index:
                    total += 1
                total += _json_string_chars(key) + 1 + _json_value_chars(child, seen=seen)
            return total
        finally:
            seen.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in seen:
            raise S3RecordSerializationError
        seen.add(identity)
        try:
            return 2 + max(0, len(value) - 1) + sum(_json_value_chars(child, seen=seen) for child in value)
        finally:
            seen.remove(identity)
    raise S3RecordSerializationError


def _check_json_record(row: Mapping[str, Any], max_record_chars: int) -> None:
    traversal_failed = False
    record_chars = 0
    try:
        record_chars = _json_value_chars(row, seen=set())
    except S3RecordSerializationError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        traversal_failed = True
    if traversal_failed:
        raise S3RecordSerializationError from None
    if record_chars > max_record_chars:
        raise S3RecordSizeLimitError


def _csv_scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if type(value) is bool:
        return str(value)
    if type(value) is int:
        conversion_failed = False
        rendered = ""
        try:
            rendered = str(value)
        except (ValueError, TypeError, OverflowError):
            conversion_failed = True
        if conversion_failed:
            raise S3RecordSerializationError from None
        return rendered
    if type(value) is float:
        if not math.isfinite(value):
            raise S3RecordSerializationError
        conversion_failed = False
        rendered = ""
        try:
            rendered = str(value)
        except (ValueError, TypeError, OverflowError):
            conversion_failed = True
        if conversion_failed:
            raise S3RecordSerializationError from None
        return rendered
    raise S3RecordSerializationError


def _csv_record_chars(values: Sequence[str], delimiter: str) -> int:
    total = max(0, len(values) - 1) + 2
    for value in values:
        if delimiter in value or '"' in value or "\r" in value or "\n" in value:
            total += len(value) + value.count('"') + 2
        else:
            total += len(value)
    return total


def _serialize_rows_to_spool(
    rows: Iterable[Mapping[str, Any]],
    *,
    format: Literal["csv", "json", "jsonl"],
    csv_options: CSVWriteOptions,
    fieldnames: Sequence[str],
    max_object_bytes: int,
    max_record_chars: int,
    header_fields: Sequence[str] | None = None,
) -> _SerializedObject:
    """Incrementally serialize rows into an owned, bounded 8 MiB spool.

    ``fieldnames`` are the pipeline field names used to look up row values and
    validate row shape. ``header_fields`` optionally supplies the display
    labels written as the CSV header record (defaults to ``fieldnames``); rows
    always stay keyed by pipeline names.
    """
    body = cast(
        "BinaryIO",
        tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES, mode="w+b"),  # noqa: SIM115 - ownership is returned
    )
    writer = _BoundedBinaryWriter(body, max_object_bytes)
    json_encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    completed = False
    try:
        if format == "csv":
            text_writer = _EncodedTextWriter(writer, csv_options.encoding)
            csv_writer = csv.writer(text_writer, delimiter=csv_options.delimiter, lineterminator="\r\n")
            if csv_options.include_header:
                header_values = list(fieldnames) if header_fields is None else list(header_fields)
                if _csv_record_chars(header_values, csv_options.delimiter) > max_record_chars:
                    raise S3RecordSizeLimitError
                csv_writer.writerow(header_values)
            for row in rows:
                if set(row) - set(fieldnames):
                    raise S3RecordSerializationError
                values = [_csv_scalar_text(row.get(field)) for field in fieldnames]
                if _csv_record_chars(values, csv_options.delimiter) > max_record_chars:
                    raise S3RecordSizeLimitError
                csv_writer.writerow(values)
            text_writer.finalize()
        elif format in {"json", "jsonl"}:
            text_writer = _EncodedTextWriter(writer, "utf-8")
            if format == "json":
                writer.write(b"[")
            for index, row in enumerate(rows):
                _check_json_record(row, max_record_chars)
                if format == "json" and index:
                    writer.write(b",")
                try:
                    for fragment in json_encoder.iterencode(row):
                        text_writer.write(fragment)
                except S3ObjectSizeLimitError:
                    raise
                except (TypeError, ValueError, UnicodeError):
                    serialization_failed = True
                else:
                    serialization_failed = False
                if serialization_failed:
                    raise S3RecordSerializationError from None
                if format == "jsonl":
                    writer.write(b"\n")
            if format == "json":
                writer.write(b"]")
            text_writer.finalize()
        else:
            raise AssertionError(f"Unsupported S3 sink format: {format}")
        body.seek(0)
        digest = writer.digest.digest()
        serialized = _SerializedObject(
            body=body,
            size_bytes=writer.size_bytes,
            content_hash=digest.hex(),
            checksum_sha256_b64=base64.b64encode(digest).decode("ascii"),
        )
        completed = True
        return serialized
    finally:
        if not completed:
            body.close()


def _normalize_error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE.fullmatch(name) is not None else "ProviderError"


class S3ConditionalWriteRejectedError(RuntimeError):
    """A server-side S3 write condition rejected the request."""

    def __init__(self) -> None:
        super().__init__("S3 conditional write was rejected.")


class S3SinkWriteError(RuntimeError):
    """S3 definitely rejected the object write request."""

    def __init__(self) -> None:
        super().__init__("S3 object write was rejected.")


class S3WriteOutcomeUnknownError(RuntimeError):
    """A dispatched S3 request may or may not have reached durable storage."""

    def __init__(self) -> None:
        super().__init__("S3 object write outcome is unknown; reconciliation is required.")


class S3SinkPoisonedError(RuntimeError):
    """The sink cannot safely make another cumulative request."""


class S3SinkClosedError(RuntimeError):
    """The sink has already released its provider resources."""


class S3ClientCloseError(RuntimeError):
    """The detached S3 client failed during close."""


def _raise_conditional_rejected() -> Never:
    raise S3ConditionalWriteRejectedError from None


def _raise_sink_write_rejected() -> Never:
    raise S3SinkWriteError from None


def _raise_outcome_unknown() -> Never:
    raise S3WriteOutcomeUnknownError from None


def _raise_audit_integrity(error_type: str) -> Never:
    raise AuditIntegrityError(f"Failed to record S3 call in the audit trail ({error_type}).") from None


def _s3_provider_exception_types() -> tuple[type[Exception], ...]:
    """Return the SDK failures that may cross an actual S3 dispatch seam."""
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def _s3_client_error_type() -> type[Exception]:
    from botocore.exceptions import ClientError

    return cast("type[Exception]", ClientError)


@observation_boundary(
    tier=3,
    source="botocore ClientError.response payload (AWS API error shape, not ELSPETH-owned)",
    source_param="error",
    suppresses=("R5",),
    invariant=(
        "returns None unless error is a genuine botocore ClientError whose response carries "
        "a Mapping Error.Code (str) and a Mapping ResponseMetadata.HTTPStatusCode (int); "
        "never raises on a malformed or foreign exception shape"
    ),
)
def _client_error_evidence(error: Exception) -> tuple[str, int] | None:
    """Validate the response owned by a real botocore ``ClientError``."""
    if not isinstance(error, _s3_client_error_type()):
        return None
    response = cast("_S3ClientError", error).response
    if not isinstance(response, Mapping):
        return None
    if "Error" not in response or "ResponseMetadata" not in response:
        return None
    error_payload = response["Error"]
    response_metadata = response["ResponseMetadata"]
    if not isinstance(error_payload, Mapping) or not isinstance(response_metadata, Mapping):
        return None
    if "Code" not in error_payload or "HTTPStatusCode" not in response_metadata:
        return None
    code = error_payload["Code"]
    status = response_metadata["HTTPStatusCode"]
    if not isinstance(code, str) or type(status) is not int:
        return None
    return code, status


def _provider_failure_kind(error: Exception) -> Literal["conditional", "rejected", "unknown"]:
    evidence = _client_error_evidence(error)
    if evidence is None:
        return "unknown"
    code, status = evidence
    if code in _CONDITIONAL_ERROR_CODES or status in {409, 412}:
        return "conditional"
    if code in _DEFINITE_REJECTION_CODES:
        return "rejected"
    return "unknown"


@observation_boundary(
    tier=3,
    source="AWS S3 API response payload (HeadObject/PutObject/etc., not ELSPETH-owned)",
    source_param="response",
    suppresses=("R5",),
    invariant=(
        "returns None unless ETag is present, an exact str, non-empty, bounded, and printable-ASCII; "
        "never raises on a malformed or absent ETag"
    ),
)
def _validated_etag(response: Mapping[str, Any]) -> str | None:
    if "ETag" not in response:
        return None
    value = response["ETag"]
    if not isinstance(value, str):
        return None
    if not value or len(value) > _MAX_ETAG_BYTES:
        return None
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        return None
    return value


class AWSS3Sink(BaseSink, RestagingSinkEffectCapability):
    """Write bounded cumulative CSV, JSON, or JSONL objects to AWS S3."""

    name = "aws_s3"
    determinism = Determinism.IO_WRITE
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:29fc4f64bc3de354"
    config_model = AWSS3SinkConfig
    effect_protocol_version = SINK_EFFECT_PROTOCOL_VERSION
    effect_call_type = CallType.HTTP
    supported_effect_modes = frozenset({"write"})
    supported_effect_input_kinds = frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})
    supports_resume = False

    usage_when_to_use: str = (
        "Use when one bounded run must publish its cumulative rows as one CSV, JSON, or JSONL S3 object through the "
        "default AWS credential chain with run-scoped conditional publication."
    )
    usage_when_not_to_use: str = (
        "Do not use for per-row objects, multipart or unbounded uploads, append/resume, credentials embedded in YAML, "
        "or custom S3 endpoints in ordinary Web Composer."
    )
    example_use: str = """sinks:
  results:
    plugin: aws_s3
    options:
      bucket: audit-output
      key: "runs/{{ run_id }}/results.csv"
      format: csv
      overwrite: false
      schema:
        mode: observed
"""
    capability_tags: tuple[str, ...] = ("aws", "s3", "cloud", "object-storage")

    @classmethod
    def _resolve_sink_effect_mode(
        cls,
        config: Mapping[str, object],
        *,
        purpose: SinkEffectExecutionPurpose,
    ) -> ResolvedSinkEffectMode | None:
        del cls, config, purpose
        return ResolvedSinkEffectMode("write")

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary="Writes bounded pipeline output to an AWS S3 object as CSV, JSON, or JSONL.",
            composer_hints=(
                "Use the default AWS credential chain and provide a real bucket plus key template.",
                "endpoint_url is CLI/batch-only; web-authored pipelines must omit it or set it to null.",
                "Choose format and headers for the downstream consumer; CSV can omit its header record.",
                "Set overwrite=false for conditional create and cumulative ETag-protected rewrites.",
                "Keep max_object_bytes appropriate for one bounded PutObject request.",
                "Route row serialization faults with the output on_write_failure setting.",
            ),
        )

    def configure_for_resume(self) -> None:
        raise NotImplementedError("AWSS3Sink does not support resume.") from None

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = AWSS3SinkConfig.from_dict(config, plugin_name=self.name)
        self._bucket = cfg.bucket
        self._key_template = _compile_key_template(cfg.key)
        self._format = cfg.format
        self._overwrite = cfg.overwrite
        self._csv_options = cfg.csv_options
        self._region_name = cfg.region_name
        self._endpoint_url = cfg.endpoint_url
        self._max_object_bytes = cfg.max_object_bytes
        self._max_record_chars = cfg.max_record_chars
        self._schema_config = cfg.schema_config
        init_display_headers(self, cfg.headers_mode, cfg.headers_mapping)
        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config,
            "AWSS3SinkRowSchema",
            allow_coercion=False,
        )
        self.input_schema = self._schema_class
        self.declared_required_fields = self._schema_config.get_effective_required_fields()
        self._s3_client: _S3Client | None = None
        self._closed = False

    def set_resume_field_resolution(self, resolution_mapping: dict[str, str]) -> None:
        set_resume_field_resolution(self, resolution_mapping)

    def _get_s3_client(self) -> _S3Client:
        if self._s3_client is None:
            self._s3_client = cast("_S3Client", build_s3_client(self._region_name, self._endpoint_url))
        return self._s3_client

    def _effect_key(self, ctx: RestrictedSinkEffectContext) -> str:
        rendered = self._key_template.render(
            run_id=ctx.run_id,
            timestamp=ctx.run_started_at.isoformat(),
        )
        return _validate_rendered_key(rendered)

    @staticmethod
    def _is_missing(error: Exception) -> bool:
        evidence = _client_error_evidence(error)
        if evidence is None:
            return False
        code, status = evidence
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    @staticmethod
    def _observation_from_head(response: Mapping[str, object]) -> RemoteObjectObservation:
        size: int | None = None
        if "ContentLength" in response:
            size_value = response["ContentLength"]
            if type(size_value) is not int or size_value < 0:
                raise RemoteObjectPreconditionError("S3 object inspection returned malformed ContentLength")
            size = size_value

        etag = _validated_etag(cast("Mapping[str, Any]", response))
        if "ETag" in response and etag is None:
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed ETag")

        metadata: Mapping[str, object] = {}
        if "Metadata" in response:
            metadata_value = response["Metadata"]
            if not isinstance(metadata_value, Mapping):
                raise RemoteObjectPreconditionError("S3 object inspection returned malformed Metadata")
            metadata = cast("Mapping[str, object]", metadata_value)

        content_hash = AWSS3Sink._metadata_string(metadata, "elspeth-content-sha256")
        if content_hash is not None and _SHA256_HEX.fullmatch(content_hash) is None:
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed content hash metadata")
        effect_id = AWSS3Sink._metadata_string(metadata, "elspeth-effect-id")
        if effect_id is not None and _SHA256_HEX.fullmatch(effect_id) is None:
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed effect ID metadata")
        plan_hash = AWSS3Sink._metadata_string(metadata, "elspeth-plan-hash")
        if plan_hash is not None and _SHA256_HEX.fullmatch(plan_hash) is None:
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed plan hash metadata")
        protocol_version = AWSS3Sink._metadata_string(metadata, "elspeth-protocol-version")
        if protocol_version is not None and protocol_version != SINK_EFFECT_PROTOCOL_VERSION:
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed protocol version metadata")

        checksum_b64: str | None = None
        checksum_hash: str | None = None
        if "ChecksumSHA256" in response:
            checksum_value = response["ChecksumSHA256"]
            if not isinstance(checksum_value, str):
                raise RemoteObjectPreconditionError("S3 object inspection returned malformed checksum")
            try:
                checksum_bytes = base64.b64decode(checksum_value, validate=True)
            except (binascii.Error, ValueError):
                raise RemoteObjectPreconditionError("S3 object inspection returned malformed checksum") from None
            if len(checksum_bytes) != hashlib.sha256().digest_size:
                raise RemoteObjectPreconditionError("S3 object inspection returned malformed checksum")
            checksum_b64 = checksum_value
            checksum_hash = checksum_bytes.hex()
        if content_hash is not None and checksum_hash is not None and checksum_hash != content_hash:
            raise RemoteObjectPreconditionError("S3 object inspection checksum diverges from content hash metadata")

        return RemoteObjectObservation(
            exists=True,
            etag=etag,
            content_hash=content_hash,
            size_bytes=size,
            effect_id=effect_id,
            plan_hash=plan_hash,
            protocol_version=protocol_version,
            checksum_algorithm="sha256" if checksum_b64 is not None else None,
            checksum_b64=checksum_b64,
        )

    @staticmethod
    def _metadata_string(metadata: Mapping[str, object], key: str) -> str | None:
        if key not in metadata:
            return None
        value = metadata[key]
        if not isinstance(value, str):
            raise RemoteObjectPreconditionError(f"S3 object inspection returned malformed {key} metadata")
        return value

    def _observe_effect_target(self, key: str) -> RemoteObjectObservation:
        client = self._get_s3_client()
        try:
            response = client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except _s3_provider_exception_types() as error:
            if self._is_missing(error):
                return RemoteObjectObservation(False, None, None, None)
            raise RemoteObjectPreconditionError("S3 object inspection failed before effect dispatch") from None
        if not isinstance(response, Mapping):
            raise RemoteObjectPreconditionError("S3 object inspection returned malformed evidence")
        return self._observation_from_head(response)

    def inspect_effect(
        self,
        request: SinkEffectInspectionRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectInspection:
        key = self._effect_key(ctx)
        target = f"s3://{self._bucket}/{key}"
        observation = self._observe_effect_target(key)
        # No rejection here: an existing object's content identity is not yet
        # known (the staged body doesn't exist until prepare_effect). The
        # overwrite=False guard now lives in prepare_remote_object's decision
        # block, where it can distinguish an idempotent re-affirmation from a
        # genuine collision instead of rejecting on existence alone.
        return inspect_remote_object(
            provider="aws_s3",
            target=target,
            request=request,
            observation=observation,
        )

    def _preflight_effect_members(
        self,
        effect_input: SinkEffectPipelineMembersInput,
    ) -> tuple[list[dict[str, Any]], tuple[int, ...], tuple[int, ...], tuple[DiversionAttribution, ...]]:
        accepted: list[int] = []
        diverted: list[int] = []
        diversion_attribution: list[DiversionAttribution] = []
        diverted_keys: set[tuple[str, str]] = set()
        for member in effect_input.members:
            row = deep_thaw(member.row)
            probe_fields = self._get_fieldnames_from_schema_or_rows([row])
            probe_rows = apply_display_headers(self, [row]) if self._format in {"json", "jsonl"} else [row]
            try:
                serialized = _serialize_rows_to_spool(
                    probe_rows,
                    format=self._format,
                    csv_options=self._csv_options,
                    fieldnames=probe_fields,
                    max_object_bytes=1024 * 1024 * 1024,
                    max_record_chars=self._max_record_chars,
                )
            except S3RecordSizeLimitError:
                reason = "record exceeds configured character limit"
                self._divert_row(row, row_index=member.ordinal, reason=reason)
                diverted.append(member.ordinal)
                diversion_attribution.append(build_diversion_attribution(ordinal=member.ordinal, reason=reason))
                diverted_keys.add((member.token_id, member.row_id))
            except S3RecordSerializationError:
                reason = "CSV record could not be encoded safely" if self._format == "csv" else "JSON record could not be serialized safely"
                self._divert_row(row, row_index=member.ordinal, reason=reason)
                diverted.append(member.ordinal)
                diversion_attribution.append(build_diversion_attribution(ordinal=member.ordinal, reason=reason))
                diverted_keys.add((member.token_id, member.row_id))
            else:
                serialized.close()
                accepted.append(member.ordinal)
        rows = [
            deep_thaw(member.row)
            for member in effect_input.target_snapshot_members
            if (member.token_id, member.row_id) not in diverted_keys
        ]
        return rows, tuple(accepted), tuple(diverted), tuple(diversion_attribution)

    def _staged_effect_body(
        self,
        effect_input: SinkEffectPipelineMembersInput,
    ) -> tuple[Any, tuple[int, ...], tuple[int, ...], tuple[Any, ...]]:
        rows, accepted, diverted, diversion_attribution = self._preflight_effect_members(effect_input)
        data_fields = self._get_fieldnames_from_schema_or_rows(rows)
        display_fields = self._display_fieldnames(data_fields)
        displayed_rows = apply_display_headers(self, rows) if self._format in {"json", "jsonl"} else rows
        serialized = _serialize_rows_to_spool(
            displayed_rows,
            format=self._format,
            csv_options=self._csv_options,
            fieldnames=data_fields if self._format == "csv" else display_fields,
            max_object_bytes=self._max_object_bytes,
            max_record_chars=self._max_record_chars,
            header_fields=display_fields if self._format == "csv" else None,
        )
        return serialized, accepted, diverted, diversion_attribution

    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        del ctx
        if type(request.effect_input) is not SinkEffectPipelineMembersInput:
            raise TypeError("AWSS3Sink effects require pipeline member input")
        serialized, accepted, diverted, diversion_attribution = self._staged_effect_body(request.effect_input)

        def chunks() -> Iterator[bytes]:
            serialized.body.seek(0)
            while chunk := serialized.body.read(_WRITE_CHUNK_BYTES):
                yield chunk

        evidence = request.inspection.evidence
        predecessor: ArtifactDescriptor | None = None
        predecessor_declared = evidence["predecessor_declared"]
        if predecessor_declared is True:
            observed_hash = evidence["observed_content_hash"]
            observed_size = evidence["observed_size"]
            if not isinstance(observed_hash, str) or type(observed_size) is not int:
                serialized.close()
                raise RemoteObjectPreconditionError("S3 predecessor inspection lacks exact content identity")
            predecessor = ArtifactDescriptor(
                artifact_type="file",
                path_or_uri=request.inspection.reference,
                content_hash=observed_hash,
                size_bytes=observed_size,
            )
        try:
            return prepare_remote_object(
                effect_id=request.effect_id,
                provider="aws_s3",
                inspection=request.inspection,
                body_chunks=chunks(),
                format_name=self._format,
                max_bytes=self._max_object_bytes,
                accepted_ordinals=accepted,
                diverted_ordinals=diverted,
                predecessor_descriptor=predecessor,
                checksum_algorithm="sha256",
                allow_replace=self._overwrite,
                diversion_attribution=diversion_attribution,
            )
        except RemoteObjectCollisionError:
            raise S3ConditionalWriteRejectedError from None
        finally:
            serialized.close()

    def restage_effect(
        self,
        plan: SinkEffectPlan,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> None:
        """Rebuild this plan's staged body if the spool lost it (repair only).

        A present stage short-circuits before any re-serialization; a missing
        one is re-derived from durable members and verified against the
        plan-sealed staged hash, failing closed on divergence.
        """
        del ctx
        if type(effect_input) is not SinkEffectPipelineMembersInput:
            raise TypeError("AWSS3Sink effects require pipeline member input")
        if not remote_stage_missing(plan, provider="aws_s3"):
            return
        serialized, accepted, diverted, _diversion_attribution = self._staged_effect_body(effect_input)

        def chunks() -> Iterator[bytes]:
            serialized.body.seek(0)
            while chunk := serialized.body.read(_WRITE_CHUNK_BYTES):
                yield chunk

        try:
            restage_remote_object(
                plan,
                provider="aws_s3",
                body_chunks=chunks(),
                max_bytes=self._max_object_bytes,
                accepted_ordinals=accepted,
                diverted_ordinals=diverted,
            )
        finally:
            serialized.close()

    def commit_effect(
        self,
        plan: SinkEffectPlan,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectCommitResult:
        evidence, stage = validate_remote_plan(plan, provider="aws_s3", require_stage=True)
        require_commit_authority(evidence, overwrite=self._overwrite)
        expected_target = f"s3://{self._bucket}/{self._effect_key(ctx)}"
        if evidence.target != expected_target:
            raise RemoteObjectPreconditionError("S3 effect target diverges from the configured run target")
        key = evidence.target.removeprefix(f"s3://{self._bucket}/")
        if not key or f"s3://{self._bucket}/{key}" != evidence.target:
            raise RemoteObjectPreconditionError("S3 effect target does not match configured bucket")
        client = self._get_s3_client()
        with stage.open("rb") as body:
            put_request: dict[str, object] = {
                "Bucket": self._bucket,
                "Key": key,
                "Body": body,
                "ContentLength": evidence.staged_size,
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(evidence.staged_hash)).decode("ascii"),
                "Metadata": {
                    "elspeth-content-sha256": evidence.staged_hash,
                    "elspeth-effect-id": plan.effect_id,
                    "elspeth-plan-hash": plan.plan_hash,
                    "elspeth-protocol-version": SINK_EFFECT_PROTOCOL_VERSION,
                },
            }
            if evidence.precondition == "if_none_match":
                put_request["IfNoneMatch"] = "*"
            else:
                put_request["IfMatch"] = evidence.predecessor_etag
            try:
                response = client.put_object(**put_request)
            except _s3_provider_exception_types() as error:
                failure_kind = _provider_failure_kind(error)
                if failure_kind == "conditional":
                    raise S3ConditionalWriteRejectedError from None
                if failure_kind == "rejected":
                    raise S3SinkWriteError from None
                raise S3WriteOutcomeUnknownError from None
            if not isinstance(response, Mapping) or _validated_etag(response) is None:
                _raise_outcome_unknown()
        return remote_commit_result(plan, evidence)

    def reconcile_effect(
        self,
        plan: SinkEffectPlan,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectReconcileResult:
        evidence, _stage = validate_remote_plan(plan, provider="aws_s3", require_stage=False)
        expected_target = f"s3://{self._bucket}/{self._effect_key(ctx)}"
        if evidence.target != expected_target:
            raise RemoteObjectPreconditionError("S3 effect target diverges from the configured run target")
        key = evidence.target.removeprefix(f"s3://{self._bucket}/")
        if not key or f"s3://{self._bucket}/{key}" != evidence.target:
            raise RemoteObjectPreconditionError("S3 effect target does not match configured bucket")
        return reconcile_remote_observation(plan, evidence, self._observe_effect_target(key))

    def _get_fieldnames_from_schema_or_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[str]:
        ordered_keys: list[str] = []
        seen_keys: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen_keys:
                    seen_keys.add(key)
                    ordered_keys.append(key)
        if self._schema_config.is_observed:
            return ordered_keys
        if self._schema_config.fields:
            declared = [field.name for field in self._schema_config.fields]
            if self._schema_config.mode == "flexible":
                declared_set = set(declared)
                return [*declared, *(key for key in ordered_keys if key not in declared_set)]
            return declared
        return ordered_keys

    def _display_fieldnames(self, data_fields: Sequence[str]) -> list[str]:
        display_map = get_effective_display_headers(self)
        if display_map is None:
            return list(data_fields)
        if self._headers_mode is HeaderMode.CUSTOM:
            missing = [field for field in data_fields if field not in display_map]
            if missing:
                raise ValueError("CUSTOM header mode must map every S3 output field")
        result: list[str] = []
        for field in data_fields:
            display_field = field
            if field in display_map:
                display_field = display_map[field]
            result.append(display_field)
        return result

    def write(self, rows: list[dict[str, Any]], ctx: SinkContext) -> SinkWriteResult:
        del rows, ctx
        raise RuntimeError("AWSS3Sink publication requires the recoverable sink effect coordinator") from None

    def flush(self) -> None:
        """PutObject is synchronous, so there is no deferred data to flush."""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client = self._s3_client
        self._s3_client = None
        if client is not None:
            try:
                client.close()
            except _s3_provider_exception_types() as error:
                error_type = _normalize_error_type(error)
                raise S3ClientCloseError(f"Failed to close S3 client ({error_type}).") from None
