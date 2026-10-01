"""Strict, I/O-free configuration and wire admission for Power Automate v1.

Flow response envelopes are external data. Admission produces owned immutable
values and closed diagnostic codes; row candidates remain external until the
source applies its ordinary row validation policy.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self
from urllib.parse import SplitResult, parse_qsl, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ModelWrapValidatorHandler,
    SecretStr,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.identifiers import validate_field_names
from elspeth.contracts.json_parser import check_json_depth, parse_json_strict
from elspeth.contracts.source_read_verification import MAX_JSON_DEPTH
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.plugins.infrastructure.clients.fingerprinting import is_sensitive_query_param
from elspeth.plugins.infrastructure.clients.json_utils import contains_non_finite
from elspeth.plugins.infrastructure.config_base import DataPluginConfig, NormalizedFieldMappingOption, declared_source_schema_field_names

PROTOCOL = "elspeth.power-automate.v1"
POWER_AUTOMATE_AUDIENCE = "https://service.flow.microsoft.com/"
POWER_AUTOMATE_SCOPE = "https://service.flow.microsoft.com/.default"
MAX_URL_BYTES = 16384
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_KEYS = frozenset(
    {
        "sig",
        "signature",
        "secret",
        "secret_ref",
        "secret_scope",
        "client_secret",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "code",
    }
)

PowerAutomateErrorCode = Literal[
    "invalid_encoding",
    "invalid_json",
    "duplicate_key",
    "non_finite",
    "depth_exceeded",
    "invalid_envelope",
    "invalid_protocol",
    "invalid_identifier",
    "page_size_exceeded",
    "invalid_state",
    "identity_mismatch",
    "operation_refused",
    "invalid_url",
    "origin_mismatch",
    "invalid_origin",
    "credential_query",
    "missing_signature",
]


class PowerAutomateProtocolError(ValueError):
    """Value-free boundary failure; external bytes never enter diagnostics."""

    def __init__(self, code: PowerAutomateErrorCode) -> None:
        super().__init__(code)
        self.code = code


def _bounded_identifier(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise PowerAutomateProtocolError("invalid_identifier")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise PowerAutomateProtocolError("invalid_identifier") from None
    if size > limit:
        raise PowerAutomateProtocolError("invalid_identifier")
    return value


def _split_https_url(value: str) -> SplitResult:
    try:
        if len(value.encode("utf-8")) > MAX_URL_BYTES or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise PowerAutomateProtocolError("invalid_url")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "#" in value
            or "\\" in value
            or "*" in parsed.netloc
            or "%" in parsed.netloc
        ):
            raise PowerAutomateProtocolError("invalid_url")
        parsed.hostname.encode("ascii")
    except (UnicodeError, ValueError):
        raise PowerAutomateProtocolError("invalid_url") from None
    return parsed


def normalize_allowed_origin(value: str) -> str:
    """Normalize a literal HTTPS443 origin without network discovery."""
    parsed = _split_https_url(value)
    if parsed.path or parsed.query or "?" in value:
        raise PowerAutomateProtocolError("invalid_origin")
    host = parsed.hostname
    assert host is not None
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}"


def validate_trigger_url(value: str, *, allowed_origin: str, sas: bool) -> str:
    """Admit the original URL before credentials can attach; never rewrite it."""
    parsed = _split_https_url(value)
    host = parsed.hostname
    assert host is not None
    authority = f"[{host}]" if ":" in host else host
    if f"https://{authority}" != normalize_allowed_origin(allowed_origin):
        raise PowerAutomateProtocolError("origin_mismatch")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if sas:
        signatures = [item for name, item in query if name.casefold() == "sig"]
        if len(signatures) != 1 or not signatures[0].strip():
            raise PowerAutomateProtocolError("missing_signature")
    elif any(is_sensitive_query_param(name) or name.casefold().replace("-", "_") in _SECRET_KEYS for name, _ in query):
        raise PowerAutomateProtocolError("credential_query")
    return value


class _AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class PowerAutomateSASAuth(_AuthModel):
    method: Literal["sas_url"] = Field(description="Authenticate with the signed HTTP trigger callback URL stored as a secret.")
    trigger_url_secret: SecretStr = Field(
        description="Complete signed HTTPS callback URL copied from the flow; supply it through an authorized secret reference."
    )

    @field_validator("trigger_url_secret")
    @classmethod
    def _nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential_required")
        return value


class PowerAutomateServicePrincipalAuth(_AuthModel):
    method: Literal["service_principal"] = Field(description="Authenticate with an Entra service principal admitted by the HTTP trigger.")
    tenant_id: StrictStr = Field(min_length=1, description="Entra tenant ID used to acquire a Power Automate access token.")
    client_id: StrictStr = Field(min_length=1, description="Application client ID of the service principal allowed to invoke this flow.")
    client_secret: SecretStr = Field(description="Service principal credential supplied through an authorized secret reference.")

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _nonempty_id(cls, value: str) -> str:
        return _bounded_identifier(value, 256)

    @field_validator("client_secret")
    @classmethod
    def _nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential_required")
        return value


class PowerAutomateManagedIdentityAuth(_AuthModel):
    method: Literal["managed_identity"] = Field(description="Authenticate with an explicitly selected user-assigned managed identity.")
    client_id: StrictStr = Field(min_length=1, description="Client ID of the user-assigned managed identity allowed to invoke this flow.")

    @field_validator("client_id")
    @classmethod
    def _nonempty_id(cls, value: str) -> str:
        return _bounded_identifier(value, 256)


PowerAutomateAuth = Annotated[
    PowerAutomateSASAuth | PowerAutomateServicePrincipalAuth | PowerAutomateManagedIdentityAuth,
    Field(discriminator="method"),
]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class _PowerAutomateOptions(DataPluginConfig):
    _component_type_exempt: ClassVar[bool] = True
    model_config = ConfigDict(hide_input_in_errors=True)

    trigger_url: StrictStr | None = Field(
        default=None,
        description="Unsigned HTTPS trigger URL for OAuth authentication; omit when auth supplies a signed callback URL.",
    )
    allowed_origin: StrictStr = Field(
        description="Exact approved HTTPS origin for the flow, without a path or query; every request must stay on this origin."
    )
    timeout_seconds: float = Field(
        default=90,
        gt=0,
        le=110,
        allow_inf_nan=False,
        description="Absolute HTTP exchange deadline in seconds, including connection and bounded response reading; maximum 110 seconds.",
    )
    max_request_body_bytes: PositiveInt = Field(
        default=1048576, description="Maximum UTF-8 byte size of a serialized read, status or write request before dispatch."
    )

    @model_validator(mode="wrap")
    @classmethod
    def _safe_config_errors(cls, value: object, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Pydantic field paths and union diagnostics can contain authored values."""
        try:
            return handler(value)
        except ValueError:
            raise ValueError("invalid_configuration") from None

    @field_validator("allowed_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        return normalize_allowed_origin(value)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _timeout(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("invalid_timeout")
        if value <= 0 or value > 110 or not math.isfinite(value):
            raise ValueError("invalid_timeout")
        return value


def _check_json_depth(text: str) -> None:
    """Resource bound before decoding; JSON grammar remains the shared parser's job."""
    try:
        check_json_depth(text, max_depth=MAX_JSON_DEPTH)
    except ValueError:
        raise PowerAutomateProtocolError("depth_exceeded") from None


def _query_has_credentials(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(
            is_sensitive_query_param(key) or key.casefold().replace("-", "_") in _SECRET_KEYS or _query_has_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_query_has_credentials(item) for item in value)
    if isinstance(value, str):
        return "${" in value
    return False


class PowerAutomateSourceOptions[AuthT: BaseModel](_PowerAutomateOptions):
    """Shared source fields with an explicit live or archived authentication type."""

    _plugin_component_type: ClassVar[str | None] = "source"

    auth: AuthT = Field(description="Flow authentication method and its required identity or secret-backed credential fields.")

    on_validation_failure: StrictStr = Field(
        description="Destination sink for invalid row candidates, or discard to omit them with audit evidence."
    )
    field_mapping: NormalizedFieldMappingOption = Field(
        default=None, description="Rename normalized external row field names to pipeline field names before schema validation."
    )
    query: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Finite JSON selection parameters interpreted by the read flow; credentials and secret markers are forbidden.",
    )
    snapshot_id: StrictStr | None = Field(
        default=None, description="Optional existing snapshot ID to request; every returned page must retain the same snapshot ID."
    )
    snapshot_for_resume: StrictBool = Field(
        default=False,
        description="Retain admitted source rows for ordinary single-source resume; the retained snapshot is limited to 64 MiB.",
    )
    page_size: Annotated[int, Field(strict=True, ge=1, le=1000)] = Field(
        default=100, description="Maximum row candidates requested per page, from 1 to 1000; oversized pages fail before yielding rows."
    )
    max_pages: PositiveInt = Field(
        default=1000, description="Maximum pages permitted in the finite read; a continuation at the limit is refused."
    )
    max_rows: PositiveInt = Field(
        default=100000, description="Maximum row candidates across all pages, including candidates rejected during row validation."
    )
    max_response_body_bytes: PositiveInt = Field(
        default=4194304, description="Maximum decoded byte size of one read response, enforced before JSON envelope admission."
    )

    @field_validator("on_validation_failure")
    @classmethod
    def _route(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("on_validation_failure_required")
        return value.strip()

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_identifier(value, 256)

    @model_validator(mode="after")
    def _source_options(self) -> Self:
        if _query_has_credentials(self.query):
            raise ValueError("query_credentials_forbidden")
        try:
            text = canonical_json(self.query)
            _check_json_depth(text)
        except (TypeError, ValueError):
            raise ValueError("invalid_query") from None
        if len(text.encode("utf-8")) > self.max_request_body_bytes:
            raise ValueError("query_too_large")
        from elspeth.plugins.sources.field_normalization import check_declared_fields_reachable, normalize_field_name

        if self.field_mapping is not None:
            try:
                validate_field_names(tuple(self.field_mapping), "field_mapping keys")
                validate_field_names(tuple(self.field_mapping.values()), "field_mapping values")
            except ValueError:
                raise ValueError("invalid_field_mapping") from None
            if any(normalize_field_name(key) != key for key in self.field_mapping):
                raise ValueError("field_mapping_keys_must_be_normalized")
        declared = declared_source_schema_field_names(self.schema_config)
        if declared:
            try:
                check_declared_fields_reachable(
                    declared, columns=None, field_mapping=self.field_mapping, header_kind="Power Automate row names"
                )
            except ValueError:
                raise ValueError("unreachable_schema_fields") from None
        object.__setattr__(self, "query", deep_freeze(self.query))
        if self.field_mapping is not None:
            object.__setattr__(self, "field_mapping", deep_freeze(self.field_mapping))
        return self


class PowerAutomateSinkOptions[AuthT: BaseModel](_PowerAutomateOptions):
    """Shared sink fields with an explicit live or archived authentication type."""

    _plugin_component_type: ClassVar[str | None] = "sink"

    auth: AuthT = Field(description="Flow authentication method and its required identity or secret-backed credential fields.")

    fields: tuple[StrictStr, ...] = Field(
        description="Unique required input fields selected for each business delivery; only these values are published and hashed."
    )
    max_response_body_bytes: PositiveInt = Field(
        default=1048576, description="Maximum decoded byte size of one status or write receipt before strict protocol admission."
    )

    @model_validator(mode="after")
    def _selected_fields(self) -> Self:
        try:
            validate_field_names(self.fields, "fields", allow_empty_sequence=False)
        except ValueError:
            raise ValueError("invalid_selected_fields") from None
        if not set(self.fields) <= self.schema_config.get_effective_required_fields():
            raise ValueError("selected_fields_must_be_required_input")
        return self


def _validate_live_endpoint(auth: PowerAutomateAuth, trigger_url: str | None, allowed_origin: str) -> None:
    if isinstance(auth, PowerAutomateSASAuth):
        if trigger_url is not None:
            raise ValueError("mixed_trigger_url")
        validate_trigger_url(auth.trigger_url_secret.get_secret_value(), allowed_origin=allowed_origin, sas=True)
    else:
        if trigger_url is None:
            raise ValueError("trigger_url_required")
        validate_trigger_url(trigger_url, allowed_origin=allowed_origin, sas=False)


class PowerAutomateSourceConfig(PowerAutomateSourceOptions[PowerAutomateAuth]):
    """Live source admission: archived fingerprints cannot stand in for credentials."""

    _plugin_component_type: ClassVar[str | None] = "source"

    @model_validator(mode="after")
    def _endpoint_auth(self) -> Self:
        _validate_live_endpoint(self.auth, self.trigger_url, self.allowed_origin)
        return self


class PowerAutomateSinkConfig(PowerAutomateSinkOptions[PowerAutomateAuth]):
    """Live sink admission: archived fingerprints cannot stand in for credentials."""

    _plugin_component_type: ClassVar[str | None] = "sink"

    @model_validator(mode="after")
    def _endpoint_auth(self) -> Self:
        _validate_live_endpoint(self.auth, self.trigger_url, self.allowed_origin)
        return self


@dataclass(frozen=True, slots=True)
class PowerAutomateReadPage:
    snapshot_id: str
    rows: tuple[object, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", deep_freeze(self.rows))


@dataclass(frozen=True, slots=True)
class PowerAutomateApplied:
    delivery_id: str
    payload_sha256: str
    receipt_id: str
    flow_run_id: str
    state: Literal["applied"] = field(default="applied", init=False)


@dataclass(frozen=True, slots=True)
class PowerAutomateNotApplied:
    delivery_id: str
    payload_sha256: str
    state: Literal["not_applied"] = field(default="not_applied", init=False)


@dataclass(frozen=True, slots=True)
class PowerAutomateUnknown:
    delivery_id: str
    payload_sha256: str
    state: Literal["unknown"] = field(default="unknown", init=False)


@dataclass(frozen=True, slots=True)
class PowerAutomateRejected:
    delivery_id: str
    payload_sha256: str
    reason_code: Literal["validation_failed", "policy_denied", "target_conflict"]
    state: Literal["rejected"] = field(default="rejected", init=False)


PowerAutomateEffectResponse = PowerAutomateApplied | PowerAutomateNotApplied | PowerAutomateUnknown | PowerAutomateRejected


def _parse_envelope(body: bytes) -> dict[str, JsonValue]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise PowerAutomateProtocolError("invalid_encoding") from None
    _check_json_depth(text)
    parsed, error = parse_json_strict(text)
    if error is not None:
        if error.startswith("Duplicate JSON key:"):
            raise PowerAutomateProtocolError("duplicate_key")
        if error.startswith("JSON contains non-finite value:"):
            raise PowerAutomateProtocolError("non_finite")
        raise PowerAutomateProtocolError("invalid_json")
    if contains_non_finite(parsed):
        raise PowerAutomateProtocolError("non_finite")
    if not isinstance(parsed, dict):
        raise PowerAutomateProtocolError("invalid_envelope")
    if "protocol" not in parsed or parsed["protocol"] != PROTOCOL:
        raise PowerAutomateProtocolError("invalid_protocol")
    return parsed


def parse_read_response(body: bytes, *, page_size: int) -> PowerAutomateReadPage:
    """Admit a closed page while preserving arbitrary JSON row candidates."""
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("invalid_page_size")
    envelope = _parse_envelope(body)
    if set(envelope) != {"protocol", "snapshot_id", "rows", "next_cursor"}:
        raise PowerAutomateProtocolError("invalid_envelope")
    snapshot = _bounded_identifier(envelope["snapshot_id"], 256)
    rows = envelope["rows"]
    if not isinstance(rows, list):
        raise PowerAutomateProtocolError("invalid_envelope")
    if len(rows) > page_size:
        raise PowerAutomateProtocolError("page_size_exceeded")
    cursor = envelope["next_cursor"]
    return PowerAutomateReadPage(snapshot, tuple(rows), None if cursor is None else _bounded_identifier(cursor, 4096))


def parse_effect_response(
    body: bytes,
    *,
    expected_delivery_id: str,
    expected_payload_sha256: str,
    operation: Literal["status", "write"],
) -> PowerAutomateEffectResponse:
    """Admit exact state variants bound to the requested delivery and payload."""
    if operation not in ("status", "write"):
        raise PowerAutomateProtocolError("operation_refused")
    envelope = _parse_envelope(body)
    common = {"protocol", "delivery_id", "payload_sha256", "state"}
    if not common <= set(envelope):
        raise PowerAutomateProtocolError("invalid_envelope")
    delivery = envelope["delivery_id"]
    payload = envelope["payload_sha256"]
    if (
        not isinstance(delivery, str)
        or _HASH.fullmatch(delivery) is None
        or not isinstance(payload, str)
        or _HASH.fullmatch(payload) is None
    ):
        raise PowerAutomateProtocolError("invalid_identifier")
    if delivery != expected_delivery_id or payload != expected_payload_sha256:
        raise PowerAutomateProtocolError("identity_mismatch")
    state = envelope["state"]
    if state == "applied":
        if set(envelope) != common | {"receipt_id", "flow_run_id"}:
            raise PowerAutomateProtocolError("invalid_envelope")
        return PowerAutomateApplied(
            delivery, payload, _bounded_identifier(envelope["receipt_id"], 256), _bounded_identifier(envelope["flow_run_id"], 256)
        )
    if state == "not_applied":
        if operation != "status":
            raise PowerAutomateProtocolError("operation_refused")
        if set(envelope) != common:
            raise PowerAutomateProtocolError("invalid_envelope")
        return PowerAutomateNotApplied(delivery, payload)
    if state == "unknown":
        if set(envelope) != common:
            raise PowerAutomateProtocolError("invalid_envelope")
        return PowerAutomateUnknown(delivery, payload)
    if state == "rejected":
        if set(envelope) != common | {"reason_code"}:
            raise PowerAutomateProtocolError("invalid_envelope")
        reason = envelope["reason_code"]
        if reason == "validation_failed":
            return PowerAutomateRejected(delivery, payload, "validation_failed")
        if reason == "policy_denied":
            return PowerAutomateRejected(delivery, payload, "policy_denied")
        if reason == "target_conflict":
            return PowerAutomateRejected(delivery, payload, "target_conflict")
        raise PowerAutomateProtocolError("invalid_state")
    raise PowerAutomateProtocolError("invalid_state")


def selected_data_hash(data: Mapping[str, object]) -> str:
    """Hash only the selected business data using ELSPETH's canonical policy."""
    return stable_hash(data)
