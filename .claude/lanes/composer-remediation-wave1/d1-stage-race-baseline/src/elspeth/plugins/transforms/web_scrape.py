"""Web scraping transform with audit trail integration.

Fetches webpages, extracts content, and generates fingerprints for change detection.
Designed for compliance monitoring use cases with full audit trail integration.

Security Features:
- SSRF prevention (blocks private IPs, cloud metadata)
- URL scheme validation (HTTP/HTTPS only)
- Configurable timeouts
- Rate limiting support

Audit Trail:
- Records all HTTP calls via AuditedHTTPClient
- Stores request, raw response, and processed content in PayloadStore
- Generates fingerprints for change detection
"""

import ipaddress
import math
import time
from collections.abc import Mapping
from dataclasses import asdict, replace
from ipaddress import IPv4Network, IPv6Network
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import escape as escape_xml_text

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts import CallType, Determinism
from elspeth.contracts.audit import Call
from elspeth.contracts.call_data import (
    HTTPCallRequest,
    MultipartMetadata,
    MultipartPart,
    encode_multipart_form,
    encode_urlencoded_form,
    multipart_min_body_size,
)
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.contracts.plugin_capabilities import ContentTrust
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.wire_visible_identity import is_wire_visible_placeholder
from elspeth.core.security.web import (
    HTTPOrigin,
    SSRFBlockedError,
    SSRFSafeRequest,
    parse_http_origin,
    validate_allowed_http_origin,
    validate_archived_ssrf_request,
    validate_configured_url_for_ssrf,
    validate_url_for_ssrf,
)
from elspeth.core.security.web import (
    NetworkError as SSRFNetworkError,
)
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    MAX_AUDIT_QUERY_CHARS,
    MAX_AUDIT_QUERY_FIELDS,
    fingerprint_headers,
    fingerprint_params,
    fingerprint_url,
    is_sensitive_query_param,
)
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient, HTTPResponseBodyTooLargeError, HTTPResponseEncodingLimitError
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.transforms.web_scrape_auth import WebScrapeAuthConfig
from elspeth.plugins.transforms.web_scrape_errors import (
    URL_FIELD_MISSING,
    URL_NOT_A_STRING,
    BodyTooLargeError,
    ClientError,
    ForbiddenError,
    InvalidURLError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
    WebScrapeError,
    row_url_policy_refusal,
    row_url_value_refusal,
)
from elspeth.plugins.transforms.web_scrape_extraction import (
    CSSRecordsConfig,
    extract_content,
    extract_css_records,
    extract_css_records_with_provenance,
)
from elspeth.plugins.transforms.web_scrape_fingerprint import compute_fingerprint
from elspeth.plugins.transforms.web_scrape_json_extraction import JSONRecordsConfig, extract_json_records_with_provenance
from elspeth.plugins.transforms.web_scrape_links import resolve_discovered_href
from elspeth.plugins.transforms.web_scrape_pagination import (
    PageProvenance,
    PaginationConfig,
    discover_next_url,
    is_disallowed_pagination_query_param,
)
from elspeth.plugins.transforms.web_scrape_request_headers import build_request_headers
from elspeth.plugins.transforms.web_scrape_response import ResponseContentError, admit_response_content

if TYPE_CHECKING:
    from elspeth.contracts.plugin_assistance import PluginAssistance
    from elspeth.contracts.plugin_semantics import OutputSemanticDeclaration

# Audit-only fields — provenance metadata that lives in success_reason["metadata"],
# not in pipeline rows. See spec: 2026-03-21-audit-provenance-boundary-design.md
WEBSCRAPE_AUDIT_FIELDS: tuple[str, ...] = (
    "fetch_request_hash",
    "fetch_response_raw_hash",
    "fetch_response_processed_hash",
)


def _is_json_value(value: object, *, depth: int = 0) -> bool:
    """Accept only JSON values without coercing row data into new types."""
    if depth > 64:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_json_value(item, depth=depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_json_value(item, depth=depth + 1) for key, item in value.items())
    return False


class _PostRequestBody(dict[str, object]):
    """Owned JSON object, copied only after strict validation of row data."""

    def __init__(self, value: object) -> None:
        if type(value) is not dict or not _is_json_value(value):
            raise ValueError("POST body must be a JSON object")
        super().__init__(value)


def _parse_form_fields(value: object) -> tuple[tuple[str, str], ...]:
    """Copy an ordered row form after validating its complete wire shape."""
    if type(value) is not list or not 1 <= len(value) <= 256:
        raise ValueError("POST form must be a list of 1 to 256 fields")
    fields: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not dict or set(item) != {"name", "value"}:
            raise ValueError("POST form entries require name and value")
        name = item["name"]
        field_value = item["value"]
        if type(name) is not str or not name or type(field_value) is not str:
            raise ValueError("POST form fields require a nonempty string name and string value")
        fields.append((name, field_value))
    return tuple(fields)


def _parse_multipart_parts(value: object) -> tuple[MultipartPart, ...]:
    """Copy an ordered row multipart manifest; local paths are never accepted."""
    if type(value) is not list or not 1 <= len(value) <= 256:
        raise ValueError("POST multipart must be a list of 1 to 256 parts")
    parts: list[MultipartPart] = []
    for item in value:
        if type(item) is not dict:
            raise ValueError("POST multipart entries must be objects")
        keys = set(item)
        if keys == {"name", "value"}:
            parts.append(MultipartPart(name=item["name"], value=item["value"]))
        elif keys == {"name", "blob_ref", "filename", "content_type"}:
            parts.append(
                MultipartPart(
                    name=item["name"],
                    blob_ref=item["blob_ref"],
                    filename=item["filename"],
                    content_type=item["content_type"],
                )
            )
        else:
            raise ValueError("POST multipart entry must be a text value or a payload-store blob reference")
    return tuple(parts)


def _validate_cidr_entry(entry: str) -> str:
    """Validate a single ``allowed_hosts`` CIDR string at the Tier-3 config boundary.

    External-origin config (operator/composer-authored). ``ipaddress.ip_network``
    rejects malformed entries with ``ValueError``, which Pydantic surfaces as a
    config validation error — the row/run is refused, never silently coerced.
    """
    try:
        ipaddress.ip_network(entry, strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid CIDR in allowed_hosts: {entry!r}: {exc}") from exc
    return entry


# CIDR string whose well-formedness is enforced by Pydantic at validation time.
CidrStr = Annotated[str, AfterValidator(_validate_cidr_entry)]


class WebScrapeHTTPConfig(BaseModel):
    """HTTP client configuration for web scrape transform.

    Controls responsible scraping behavior: abuse contact for transparency,
    scraping reason for audit trail, and timeout for resource management.
    """

    model_config = {"extra": "forbid"}

    abuse_contact: str = Field(
        ...,
        description="Email for abuse reports (required for responsible scraping)",
    )
    scraping_reason: str = Field(
        ...,
        description="Why we're scraping (recorded in audit trail)",
    )
    timeout: int = Field(
        default=30,
        gt=0,
        description="Request timeout in seconds",
    )
    max_body_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10 MB
        gt=0,
        description=(
            "Maximum response body size in bytes. Responses exceeding this limit "
            "return an error result instead of being extracted and fingerprinted, "
            "preventing OOM on hostile or misconfigured Tier-3 endpoints (B3.10)."
        ),
    )
    max_request_body_bytes: int = Field(default=1024 * 1024, gt=0, description="Maximum encoded POST body size in bytes.")
    max_decoded_body_bytes: int | None = Field(
        default=None, gt=0, description="Maximum decoded response bytes; defaults to max_body_bytes."
    )
    max_encoded_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0, description="Maximum compressed response bytes.")
    max_decompression_ratio: int = Field(default=200, gt=0, description="Maximum decoded-to-encoded byte ratio for compressed responses.")
    # SSRF allowlist. The two scalar keywords are a closed set (declared as a
    # Literal so Pydantic validates the arm natively); the list arm is one-or-more
    # CIDR strings, each well-formedness-checked by CidrStr's AfterValidator, with
    # the empty list rejected via min_length=1. Pydantic resolves which union arm a
    # Tier-3 config value matches structurally -- no isinstance discrimination needed.
    allowed_hosts: Literal["public_only", "allow_private"] | Annotated[list[CidrStr], Field(min_length=1)] = Field(
        default="public_only",
        description="SSRF allowlist: 'public_only' (default), 'allow_private', or list of CIDR ranges",
    )
    allowed_origins: tuple[str, ...] = Field(default=(), description="Exact HTTP(S) origins permitted for initial requests and redirects.")

    @field_validator("allowed_origins")
    @classmethod
    def _validate_allowed_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        parsed = [parse_http_origin(value) for value in values]
        if len(values) != len(set(parsed)):
            raise ValueError("allowed_origins entries must be unique")
        return values

    @field_validator("abuse_contact", "scraping_reason")
    @classmethod
    def _validate_wire_visible_header(cls, v: str, info: Any) -> str:
        if not v.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        if is_wire_visible_placeholder(v):
            raise ValueError(
                f"{info.field_name} must be supplied by the operator or deployment identity; "
                "placeholder values are not valid for wire-visible HTTP headers"
            )
        # This value is sent verbatim as an X-Abuse-Contact / X-Scraping-Reason
        # request header. HTTP header values must be ASCII-encodable, so a
        # typographic character (em dash, curly quote, ellipsis) — the kind an
        # LLM composer routinely emits — would otherwise raise UnicodeEncodeError
        # deep inside the HTTP client mid-request, escape on_error row handling,
        # and abort the whole run. Reject it here so it surfaces as a clean
        # configuration error at validate() preflight, before any fetch.
        if not v.isascii():
            bad_index = next(i for i, ch in enumerate(v) if not ch.isascii())
            bad_char = v[bad_index]
            raise ValueError(
                f"{info.field_name} contains a non-ASCII character {bad_char!r} "
                f"(U+{ord(bad_char):04X}) at position {bad_index}; this value is sent "
                f"verbatim as an HTTP request header and must be ASCII-encodable. "
                f"Replace typographic punctuation (em dashes, curly quotes, ellipses) "
                f"with plain ASCII equivalents."
            )
        return v


class WebScrapeConfig(TransformDataConfig):
    """Configuration for web scrape transform."""

    # A nested secret can appear in an outer model's rejected input mapping,
    # including when validation fails on an unrelated option.
    model_config: ClassVar[ConfigDict] = ConfigDict(**TransformDataConfig.model_config, hide_input_in_errors=True)

    url_field: str | None = Field(
        default=None,
        description=(
            "Name of the row field whose value is the absolute URL to fetch. "
            "Values MUST include an explicit 'http://' or 'https://' scheme; "
            "bare hostnames (e.g. 'www.example.gov.au') are rejected at fetch "
            "time by the SSRF guard. If the upstream source emits scheme-less "
            "values, normalize them in the source data or add an upstream "
            "value_transform that prepends 'https://' before this transform."
        ),
    )
    url: str | None = Field(default=None, description="Fixed absolute HTTP(S) URL for every row; exclusive with url_field.")
    content_field: str = Field(description="Output field that receives the fetched page content.")
    fingerprint_field: str = Field(description="Output field that receives the page fingerprint.")
    method: Literal["GET", "POST"] = Field(default="GET", description="HTTP method for fetching the row URL.")
    request_json_field: str | None = Field(default=None, description="Row field containing a JSON object to send as a POST body.")
    request_form_field: str | None = Field(default=None, description="Row field containing ordered URL-encoded POST form entries.")
    request_multipart_field: str | None = Field(
        default=None, description="Row field containing ordered multipart text entries or payload-store blob references."
    )
    query: dict[str, str] = Field(default_factory=dict, description="Static URL query parameters sent with each request.")
    query_fields: dict[str, str] = Field(default_factory=dict, description="Map URL query parameter names to input row field names.")
    headers: dict[str, str] = Field(default_factory=dict, description="Validated static public-page request headers.")
    header_fields: dict[str, str] = Field(default_factory=dict, description="Map validated request header names to input row fields.")
    format: Literal["markdown", "text", "raw"] = Field(
        default="markdown",
        description="Content extraction format to emit: markdown, plain text, or raw HTML/JSON text.",
    )
    response_mode: Literal["page", "json", "xml"] = Field(
        default="page", description="Page extraction, strict JSON text, or strict XML text. JSON/XML require format raw."
    )
    accepted_mime_types: tuple[str, ...] = Field(
        default=(), max_length=16, description="Optional exact MIME allowlist that narrows the response mode's accepted types."
    )
    charset_policy: Literal["declared_or_utf8", "utf8_only"] = Field(
        default="declared_or_utf8", description="Use a supported declared charset or UTF-8 fallback, or require UTF-8."
    )
    text_separator: Annotated[
        str,
        EmittedToOutput("web_scrape joins DOM text nodes with this separator, so the value becomes part of the scraped row data"),
    ] = Field(
        default=" ",
        min_length=1,
        max_length=16,
        description="Separator inserted between DOM text nodes when format is text.",
    )
    fingerprint_mode: Literal["content", "full"] = Field(
        default="content",
        description="Whether fingerprints cover processed content only or the full fetch response context.",
    )
    strip_elements: list[str] = Field(
        default_factory=lambda: ["script", "style"],
        description="HTML element names to remove before extracting page text.",
    )
    records: CSSRecordsConfig | JSONRecordsConfig | None = Field(
        default=None,
        description=(
            "Bounded CSS-selected or JSON-path result records emitted as a row field. "
            "An href column may set resolve_url: true to emit an approved absolute URL; "
            "this requires http.allowed_origins."
        ),
    )
    pagination: PaginationConfig | None = Field(
        default=None, description="GET-only bounded next-page discovery; all pages enrich one input row and each fetch is audited."
    )
    auth: WebScrapeAuthConfig | None = Field(
        default=None,
        repr=False,
        description="Reserved Basic, Bearer or header API-key auth; execution is unavailable until response evidence is secret safe.",
        json_schema_extra={"composer_hidden": True},
    )
    http: WebScrapeHTTPConfig = Field(description="HTTP fetching policy, timeout, contact, and host allowlist settings.")

    @field_validator(
        "url_field", "content_field", "fingerprint_field", "request_json_field", "request_form_field", "request_multipart_field"
    )
    @classmethod
    def _reject_empty_field_names(cls, v: str | None, info: Any) -> str | None:
        if v == "":
            raise ValueError(f"{info.field_name} must not be empty")
        return v

    @property
    def declared_input_fields(self) -> frozenset[str]:
        fields: set[str] = set()
        if self.url_field is not None:
            fields.add(self.url_field)
        if self.request_json_field is not None:
            fields.add(self.request_json_field)
        if self.request_form_field is not None:
            fields.add(self.request_form_field)
        if self.request_multipart_field is not None:
            fields.add(self.request_multipart_field)
        fields.update(self.query_fields.values())
        fields.update(self.header_fields.values())
        return super().declared_input_fields | frozenset(fields)

    @model_validator(mode="after")
    def _validate_query_options(self) -> "WebScrapeConfig":
        duplicate_names = self.query.keys() & self.query_fields.keys()
        if duplicate_names:
            raise ValueError(f"query and query_fields repeat parameter {sorted(duplicate_names)[0]!r}")
        for name in self.query.keys() | self.query_fields.keys():
            if not name or is_sensitive_query_param(name):
                raise ValueError(f"query parameter {name!r} must be nonempty and must not carry credentials")
        for field_name in self.query_fields.values():
            if not field_name:
                raise ValueError("query_fields row field names must not be empty")
        return self

    @model_validator(mode="after")
    def _validate_header_options(self) -> "WebScrapeConfig":
        if any(not field_name for field_name in self.header_fields.values()):
            raise ValueError("header_fields row field names must not be empty")
        # A sentinel row validates names, static values, duplicates and the
        # complete header block without accepting row values at config time.
        build_request_headers(self.headers, self.header_fields, dict.fromkeys(self.header_fields.values(), "probe"))
        return self

    @model_validator(mode="after")
    def _validate_response_policy(self) -> "WebScrapeConfig":
        if isinstance(self.records, JSONRecordsConfig) and self.response_mode != "json":
            raise ValueError("JSON records require response_mode json")
        if isinstance(self.records, CSSRecordsConfig) and self.response_mode != "page":
            raise ValueError("CSS records require response_mode page")
        if self.response_mode != "page" and self.format != "raw":
            raise ValueError("JSON and XML response modes require format raw")
        if self.http.max_decoded_body_bytes is not None and self.http.max_decoded_body_bytes > self.http.max_body_bytes:
            raise ValueError("max_decoded_body_bytes cannot exceed max_body_bytes")
        for value in self.accepted_mime_types:
            parts = value.split("/")
            if len(parts) != 2 or any(
                not part or not part.isascii() or not all(ch.isalnum() or ch in ".+-" for ch in part) for part in parts
            ):
                raise ValueError("accepted_mime_types must contain exact MIME types")
            if value.lower() != value:
                raise ValueError("accepted_mime_types must use lowercase MIME types")
        if len(set(self.accepted_mime_types)) != len(self.accepted_mime_types):
            raise ValueError("accepted_mime_types entries must be unique")
        return self

    @model_validator(mode="after")
    def _validate_url_source(self) -> "WebScrapeConfig":
        if (self.url is None) == (self.url_field is None):
            raise ValueError("exactly one of url or url_field is required")
        if self.url is not None:
            allowed_hosts = self.http.allowed_hosts
            if allowed_hosts == "public_only":
                allowed_ranges: tuple[IPv4Network | IPv6Network, ...] = ()
            elif allowed_hosts == "allow_private":
                allowed_ranges = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))
            else:
                allowed_ranges = _parse_allowed_ranges(allowed_hosts)
            try:
                validate_configured_url_for_ssrf(self.url, allowed_ranges=allowed_ranges)
                validate_allowed_http_origin(self.url, tuple(parse_http_origin(value) for value in self.http.allowed_origins))
            except SSRFBlockedError as exc:
                raise ValueError(f"url: {exc}") from exc
        if self.auth is not None:
            auth_origin = self.auth.parsed_origin
            allowed_origins = tuple(parse_http_origin(value) for value in self.http.allowed_origins)
            if auth_origin not in allowed_origins:
                raise ValueError("auth.origin must be listed in http.allowed_origins")
            if self.url is not None:
                try:
                    validate_allowed_http_origin(self.url, (auth_origin,))
                except SSRFBlockedError as exc:
                    raise ValueError("fixed url must match auth.origin") from exc
        return self

    @model_validator(mode="after")
    def _validate_resolved_link_policy(self) -> "WebScrapeConfig":
        if (
            isinstance(self.records, CSSRecordsConfig)
            and any(column.resolve_url for column in self.records.columns)
            and not self.http.allowed_origins
        ):
            raise ValueError("records columns with resolve_url require http.allowed_origins")
        return self

    @model_validator(mode="after")
    def _validate_request_body_option(self) -> "WebScrapeConfig":
        body_sources = [
            field for field in (self.request_json_field, self.request_form_field, self.request_multipart_field) if field is not None
        ]
        if self.method == "POST" and len(body_sources) != 1:
            raise ValueError(
                "exactly one of request_json_field, request_form_field or request_multipart_field is required when method is POST"
            )
        if self.method == "GET" and body_sources:
            raise ValueError("request_json_field, request_form_field and request_multipart_field are only valid when method is POST")
        if self.pagination is not None:
            if self.method != "GET":
                raise ValueError("pagination is available only for GET")
            if not self.http.allowed_origins:
                raise ValueError("pagination requires explicit http.allowed_origins for every page and redirect")
        return self

    @model_validator(mode="after")
    def _reject_field_collisions(self) -> "WebScrapeConfig":
        if self.content_field == self.fingerprint_field:
            raise ValueError(f"content_field and fingerprint_field must differ, both are '{self.content_field}'")
        if self.url_field is not None and self.request_json_field == self.url_field:
            raise ValueError("request_json_field and url_field must differ")
        if self.url_field is not None and self.request_form_field == self.url_field:
            raise ValueError("request_form_field and url_field must differ")
        if self.url_field is not None and self.request_multipart_field == self.url_field:
            raise ValueError("request_multipart_field and url_field must differ")
        if self.records is not None and self.records.field in {
            self.content_field,
            self.fingerprint_field,
            "fetch_status",
            "fetch_url_final",
            "fetch_url_final_ip",
        }:
            raise ValueError("records field must differ from other web_scrape output fields")
        if self.records is not None and self.records.provenance_field in {
            self.content_field,
            self.fingerprint_field,
            "fetch_status",
            "fetch_url_final",
            "fetch_url_final_ip",
        }:
            raise ValueError("provenance_field must differ from other web_scrape output fields")
        return self

    @model_validator(mode="after")
    def _reject_option_key_names_in_schema_field_lists(self) -> "WebScrapeConfig":
        """Catch the LLM-composer footgun of listing option-key names in schema column lists.

        A bad config emitted by upstream composers has been observed listing the
        literal strings ``"url_field"``, ``"content_field"``, and
        ``"fingerprint_field"`` inside ``schema.guaranteed_fields``. Those are
        *names of WebScrapeConfig options*, not column names — what the author
        meant was to list the *values* of those options (i.e. the actual column
        names the transform reads from or writes to). The same hallucination is
        equally likely in ``schema.required_fields`` and ``schema.audit_fields``,
        which are sibling ``tuple[str, ...] | None`` column-name lists on
        ``SchemaConfig``, so this guard scans all three. At runtime the
        SchemaConfigModeContract correctly rejects the offending names, but the
        misconfiguration is detectable at plugin-validate time, so we surface it
        here for early, actionable feedback to composer authors (human or LLM).

        Degenerate case: an operator may legitimately configure a knob so that
        its value equals its key name (e.g. ``content_field: "content_field"``),
        meaning the column on the row is literally called ``content_field``. In
        that case the schema list entry is correct — the row really does carry
        a column of that name — so the guard skips entries where the configured
        value matches the key.
        """
        option_key_to_value: dict[str, str] = {
            "content_field": self.content_field,
            "fingerprint_field": self.fingerprint_field,
        }
        if self.url_field is not None:
            option_key_to_value["url_field"] = self.url_field
        if self.request_json_field is not None:
            option_key_to_value["request_json_field"] = self.request_json_field
        if self.request_form_field is not None:
            option_key_to_value["request_form_field"] = self.request_form_field
        if self.request_multipart_field is not None:
            option_key_to_value["request_multipart_field"] = self.request_multipart_field
        for name, field_name in self.query_fields.items():
            option_key_to_value[f"query_fields.{name}"] = field_name
        for name, field_name in self.header_fields.items():
            option_key_to_value[f"header_fields.{name}"] = field_name

        list_name_to_entries: dict[str, tuple[str, ...] | None] = {
            "guaranteed_fields": self.schema_config.guaranteed_fields,
            "required_fields": self.schema_config.required_fields,
            "audit_fields": self.schema_config.audit_fields,
        }

        # Collect offenders per list so the error message can tell the author
        # which list each bad entry came from.
        offenders_by_list: dict[str, list[str]] = {}
        for list_name, entries in list_name_to_entries.items():
            if entries is None:
                continue
            list_offenders = [entry for entry in entries if entry in option_key_to_value and option_key_to_value[entry] != entry]
            if list_offenders:
                offenders_by_list[list_name] = list_offenders

        if not offenders_by_list:
            return self

        bullet_lines: list[str] = []
        offender_summary_parts: list[str] = []
        for list_name, list_offenders in offenders_by_list.items():
            offender_summary_parts.append(f"{list_name}=[{', '.join(repr(entry) for entry in list_offenders)}]")
            for entry in list_offenders:
                bullet_lines.append(
                    f"  - schema.{list_name}: '{entry}' is the name of the '{entry}' "
                    f"option, not a column name; substitute the configured value "
                    f"'{option_key_to_value[entry]}'."
                )
        offender_summary = "; ".join(offender_summary_parts)
        message = (
            f"schema field-name lists contain option-key names ({offender_summary}), "
            "not column names; those strings are WebScrapeConfig option keys whose "
            "values are the actual column names this transform reads from or writes "
            "to. Replace each with the configured column name:\n" + "\n".join(bullet_lines)
        )
        raise ValueError(message)


def _parse_allowed_ranges(entries: list[str]) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse allowed_hosts list entries into ip_network objects.

    Single IPs (no /) are expanded to /32 (IPv4) or /128 (IPv6).
    Uses strict=False so "10.0.0.1/8" is accepted as "10.0.0.0/8".
    """
    networks: list[IPv4Network | IPv6Network] = []
    for entry in entries:
        network = ipaddress.ip_network(entry, strict=False)
        networks.append(network)
    return tuple(networks)


def _web_scrape_added_output_fields(
    content_field: str, fingerprint_field: str, records_field: str | None = None, provenance_field: str | None = None
) -> tuple[FieldDefinition, ...]:
    """Return the typed fields WebScrape guarantees on successful output rows."""
    fields: tuple[FieldDefinition, ...] = (
        FieldDefinition(name=content_field, field_type="str", required=True),
        FieldDefinition(name=fingerprint_field, field_type="str", required=True),
        FieldDefinition(name="fetch_status", field_type="int", required=True),
        FieldDefinition(name="fetch_url_final", field_type="str", required=True),
        FieldDefinition(name="fetch_url_final_ip", field_type="str", required=True),
    )
    if records_field is not None:
        fields = (*fields, FieldDefinition(name=records_field, field_type="any", required=True))
    if provenance_field is not None:
        fields = (*fields, FieldDefinition(name=provenance_field, field_type="any", required=True))
    return fields


def _build_web_scrape_output_schema_config(
    schema_config: SchemaConfig,
    *,
    content_field: str,
    fingerprint_field: str,
    records_field: str | None = None,
    provenance_field: str | None = None,
) -> SchemaConfig:
    """Build the typed output contract for WebScrape's pass-through enrichment."""
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _web_scrape_added_output_fields(content_field, fingerprint_field, records_field, provenance_field)
    field_by_name.update((field.name, field) for field in added_fields)

    base_guaranteed = set(schema_config.guaranteed_fields or ())
    output_guaranteed = base_guaranteed | {field.name for field in added_fields}

    return SchemaConfig(
        # Observed input still has a known output minimum after enrichment.
        mode=schema_config.mode if schema_config.fields is not None else "flexible",
        fields=tuple(field_by_name.values()),
        guaranteed_fields=tuple(sorted(output_guaranteed)),
        audit_fields=schema_config.audit_fields,
        required_fields=schema_config.required_fields,
    )


def _build_web_scrape_output_semantics(
    *,
    content_field: str,
    format: str,
    text_separator: str,
    response_mode: str = "page",
) -> "OutputSemanticDeclaration":
    """Map WebScrapeConfig values to declared output facts for the content field."""
    from elspeth.contracts.plugin_semantics import (
        ContentKind,
        FieldSemanticFacts,
        OutputSemanticDeclaration,
        SemanticValueType,
        TextFraming,
    )

    # ``value_type`` is STR for every recognized format, and that is a fact this
    # transform genuinely knows: ``process`` calls ``content.encode()`` before
    # assigning ``output[content_field]``, so a non-str value could not reach
    # the row. Leaving it UNKNOWN was an under-declaration, and an abstaining
    # dimension downgrades an otherwise-SATISFIED edge to advisory UNKNOWN for
    # any consumer that constrains it (ADR-039: abstention cannot be graded by
    # the facts).
    value_type = SemanticValueType.STR
    if format == "markdown":
        kind = ContentKind.MARKDOWN
        framing = TextFraming.LINE_COMPATIBLE
        fact_code = "web_scrape.content.markdown"
    elif format == "raw":
        # JSON/XML text has no matching raw-markup member in the closed
        # semantic vocabulary; do not claim that it is HTML.
        kind = ContentKind.HTML_RAW if response_mode == "page" else ContentKind.UNKNOWN
        # UNCONSTRAINED, not NOT_TEXT: the raw value is the fetched page
        # verbatim — a str whose framing is whatever the server sent, which no
        # configuration settles. That is the UNCONSTRAINED claim by definition.
        # NOT_TEXT positively claims the value is not text at all, which was
        # false of a str of HTML and made ``raw -> document`` (archive this
        # page to a file) a false authoring CONFLICT (elspeth-24c04df25f).
        # HTML_RAW on the kind axis already says what the text IS.
        framing = TextFraming.UNCONSTRAINED
        fact_code = "web_scrape.content.raw_html" if response_mode == "page" else f"web_scrape.content.raw_{response_mode}"
    elif format == "text":
        kind = ContentKind.PLAIN_TEXT
        # CR as well as LF: ``sink:text`` diverts on either ("Text values cannot
        # contain CR or LF record separators", text_sink.py), so a separator
        # carrying only CR would otherwise declare COMPACT and still divert.
        # ``extract_content`` normalises intra-node CR/LF away on exactly this
        # condition, which is what makes the COMPACT claim true rather than
        # merely intended.
        if "\n" in text_separator or "\r" in text_separator:
            framing = TextFraming.NEWLINE_FRAMED
            fact_code = "web_scrape.content.newline_framed_text"
        else:
            framing = TextFraming.COMPACT
            fact_code = "web_scrape.content.compact_text"
    else:
        # Unknown format value — let the schema layer handle it.
        # Returning UNKNOWN here is honest: we don't know. ``format`` is a
        # Literal, so this branch is defensive; abstaining on every dimension
        # keeps it from asserting anything about a shape it did not produce.
        kind = ContentKind.UNKNOWN
        framing = TextFraming.UNKNOWN
        value_type = SemanticValueType.UNKNOWN
        fact_code = "web_scrape.content.unknown_format"

    return OutputSemanticDeclaration(
        fields=(
            FieldSemanticFacts(
                field_name=content_field,
                content_kind=kind,
                text_framing=framing,
                value_type=value_type,
                fact_code=fact_code,
                configured_by=("format", "text_separator"),
            ),
        ),
    )


def _final_response_ip(response: httpx.Response) -> str:
    """Extract the final IP-pinned destination from an SSRF-safe response."""
    try:
        final_host = response.request.url.host
    except RuntimeError as e:
        raise FrameworkBugError("SSRF-safe HTTP response has no request; cannot record final resolved IP.") from e

    if final_host is None:
        raise FrameworkBugError("SSRF-safe HTTP response request URL has no host; cannot record final resolved IP.")

    try:
        ipaddress.ip_address(final_host)
    except ValueError as e:
        raise FrameworkBugError(
            f"SSRF-safe HTTP response request host {final_host!r} is not an IP address; "
            "AuditedHTTPClient must return the IP-pinned final request."
        ) from e

    return final_host


class WebScrapeTransform(BaseTransform):
    """Fetch webpages, extract content, generate fingerprints.

    Designed for compliance monitoring use cases. Features:
    - Security: SSRF prevention, URL validation
    - Audit: Full request/response recording
    - Extraction: HTML → Markdown, Text, or Raw
    - Fingerprinting: Change detection with normalization

    Configuration:
        url: Fixed absolute HTTP(S) URL for every row; exclusive with url_field.
        url_field: Field containing URL to fetch. Values MUST include an
            explicit 'http://' or 'https://' scheme; bare hostnames such as
            'www.example.gov.au' are rejected by the SSRF guard with
            ``SSRFBlockedError: URL is missing a scheme``. If the source
            emits scheme-less values, fix them in the source data or
            prepend the scheme via an upstream value_transform.
        query: Static, non-credential URL query parameters.
        query_fields: Map query parameter names to input row field names.
        content_field: Field to store extracted content
        fingerprint_field: Field to store content fingerprint
        format: Output format ("markdown", "text", "raw")
        text_separator: Separator between DOM text nodes when format is text
        fingerprint_mode: Fingerprinting mode ("content", "full")
        strip_elements: HTML tags to remove (default: ["script", "style"])
        http:
            abuse_contact: Email for abuse reports (required)
            scraping_reason: Why we're scraping (required)
            timeout: Request timeout in seconds (default: 30)

    Error Handling (follows LLM plugin pattern):
        - Retryable errors (5xx, 429, network): Re-raised for engine retry
        - Non-retryable errors (4xx, SSRF): Return TransformResult.error()

    Example:
        transforms:
          - plugin: web_scrape
            options:
              schema: {mode: observed}
              url_field: url
              content_field: page_content
              fingerprint_field: page_fingerprint
              format: markdown
              text_separator: "\n"  # only used with format: text
              http:
                abuse_contact: compliance@example.com
                scraping_reason: Regulatory monitoring
    """

    # url_field is an INPUT column when the URL varies by row; url is fixed
    # node configuration. These two keys choose where fetched content is written.
    output_naming_config_keys = frozenset({"content_field", "fingerprint_field"})
    name = "web_scrape"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:eb90d8e44c6ff93d"
    config_model = WebScrapeConfig
    passes_through_input = True
    fetches_http = True
    content_trust = ContentTrust.UNTRUSTED
    capability_tags: tuple[str, ...] = ("http", "network", "scraping")

    usage_when_to_use = (
        "Use when a node has a fixed public HTTP(S) URL or each row carries one, and you need an audited fetch, "
        "Markdown or plain text extraction, and a change fingerprint. Map row fields into URL query parameters "
        "or validated request headers "
        "for searches against a fixed site. A read-only POST can send a "
        "row's JSON object or ordered form fields to a public data endpoint. A GET can follow bounded CSS next links "
        "or HTTP Link headers when exact allowed origins are configured. Returned remote content is untrusted before "
        "LLM consumption, so apply the appropriate prompt-injection control first."
    )
    usage_when_not_to_use = (
        "Not for authenticated APIs or binary documents. Use a purpose-built authenticated API "
        "integration for APIs, or blob_fetch when the workflow must preserve original document bytes."
    )
    example_use = (
        "transform:\n"
        "  plugin: web_scrape\n"
        "  options:\n"
        "    url_field: page_url\n"
        "    content_field: page_markdown\n"
        "    fingerprint_field: page_fingerprint\n"
        "    format: markdown\n"
        "    http:\n"
        "      abuse_contact: catalogue-ops@example.org\n"
        "      scraping_reason: Audited public policy monitoring\n"
        "      allowed_hosts: public_only\n"
        "    schema: {mode: observed}"
    )

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 forward invariant."""
        return {
            "schema": {"mode": "observed"},
            "url_field": "web_scrape_probe_url",
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {
                "abuse_contact": "invariants@example.com",
                "scraping_reason": "ADR-009 invariant probe",
                "allowed_hosts": ["93.184.216.34/32"],
            },
        }

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)

        # Parse and validate config
        cfg = WebScrapeConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        # Required fields
        self._url_field = cfg.url_field
        self._url = cfg.url
        self._content_field = cfg.content_field
        self._fingerprint_field = cfg.fingerprint_field
        self._method = cfg.method
        self._request_json_field = cfg.request_json_field
        self._request_form_field = cfg.request_form_field
        self._request_multipart_field = cfg.request_multipart_field
        self._query = cfg.query
        self._query_fields = cfg.query_fields
        self._headers = cfg.headers
        self._header_fields = cfg.header_fields
        self._records = cfg.records
        self._pagination = cfg.pagination

        # Declare output fields for centralized collision detection in TransformExecutor.
        self.declared_output_fields = frozenset(
            [
                cfg.content_field,
                cfg.fingerprint_field,
                "fetch_status",
                "fetch_url_final",
                "fetch_url_final_ip",
                *([cfg.records.field] if cfg.records is not None else []),
                *([cfg.records.provenance_field] if cfg.records is not None and cfg.records.provenance_field is not None else []),
            ]
        )
        input_options: dict[str, str] = {}
        if cfg.url_field is not None:
            input_options["url_field"] = cfg.url_field
        if cfg.request_json_field is not None:
            input_options["request_json_field"] = cfg.request_json_field
        if cfg.request_form_field is not None:
            input_options["request_form_field"] = cfg.request_form_field
        if cfg.request_multipart_field is not None:
            input_options["request_multipart_field"] = cfg.request_multipart_field
        input_options.update({f"query_fields.{name}": field_name for name, field_name in cfg.query_fields.items()})
        input_options.update({f"header_fields.{name}": field_name for name, field_name in cfg.header_fields.items()})
        self._reject_input_options_naming_created_fields(input_options)

        # Format and fingerprint mode
        self._format = cfg.format
        self._text_separator = cfg.text_separator
        self._fingerprint_mode = cfg.fingerprint_mode

        # HTTP config -- validated by WebScrapeHTTPConfig sub-model
        self._abuse_contact = cfg.http.abuse_contact
        self._scraping_reason = cfg.http.scraping_reason
        self._timeout = cfg.http.timeout
        self._max_body_bytes = cfg.http.max_body_bytes
        self._max_decoded_body_bytes = cfg.http.max_decoded_body_bytes or cfg.http.max_body_bytes
        self._max_encoded_body_bytes = cfg.http.max_encoded_body_bytes
        self._max_decompression_ratio = cfg.http.max_decompression_ratio
        self._max_request_body_bytes = cfg.http.max_request_body_bytes
        self._response_mode = cfg.response_mode
        self._accepted_mime_types = cfg.accepted_mime_types
        self._charset_policy = cfg.charset_policy
        self._allowed_origins: tuple[HTTPOrigin, ...] = tuple(parse_http_origin(value) for value in cfg.http.allowed_origins)
        self._auth_header = cfg.auth.header() if cfg.auth is not None else None
        self._request_allowed_origins: tuple[HTTPOrigin, ...] = (cfg.auth.parsed_origin,) if cfg.auth is not None else self._allowed_origins

        # Compute allowed_ranges from allowed_hosts config
        allowed_hosts = cfg.http.allowed_hosts
        if allowed_hosts == "public_only":
            self._allowed_ranges: tuple[IPv4Network | IPv6Network, ...] = ()
        elif allowed_hosts == "allow_private":
            self._allowed_ranges = (
                ipaddress.ip_network("0.0.0.0/0"),
                ipaddress.ip_network("::/0"),
            )
        else:
            # Type is Literal[...] | list[CidrStr]; the two keyword arms are handled
            # above, so the remaining arm is the validated CIDR list.
            self._allowed_ranges = _parse_allowed_ranges(allowed_hosts)

        # Element stripping
        self._strip_elements = cfg.strip_elements

        # Schema
        if cfg.schema_config is None:
            raise RuntimeError("WebScrapeTransform requires schema_config")
        self.input_schema = create_schema_from_config(
            cfg.schema_config,
            "WebScrapeInput",
            allow_coercion=False,
        )
        self._output_schema_config = _build_web_scrape_output_schema_config(
            cfg.schema_config,
            content_field=cfg.content_field,
            fingerprint_field=cfg.fingerprint_field,
            records_field=cfg.records.field if cfg.records is not None else None,
            provenance_field=cfg.records.provenance_field if cfg.records is not None else None,
        )
        self.output_schema = create_schema_from_config(
            self._output_schema_config,
            "WebScrapeOutput",
            allow_coercion=False,
        )

    def output_semantics(self) -> "OutputSemanticDeclaration":
        return _build_web_scrape_output_semantics(
            content_field=self._content_field,
            format=self._format,
            text_separator=self._text_separator,
            response_mode=self._response_mode,
        )

    @classmethod
    def get_agent_assistance(
        cls,
        *,
        issue_code: str | None = None,
    ) -> "PluginAssistance | None":
        from elspeth.contracts.plugin_assistance import (
            PluginAssistance,
            PluginAssistanceExample,
        )

        if issue_code is None:
            return PluginAssistance(
                plugin_name="web_scrape",
                issue_code=None,
                summary="Fetch a fixed or row-provided URL with SSRF protection, audit recording, and content-fingerprinting. GET can map row fields into query parameters and safe headers; POST sends JSON, URL-encoded form, or bounded multipart fields from a row. Output formats: raw, text, markdown.",
                composer_hints=(
                    "web_scrape is a transform, not a source: set exactly one of url (fixed node address) or url_field (row URL); it writes content_field.",
                    "For read-only POST data retrieval, set method: POST and exactly one of request_json_field, request_form_field, or request_multipart_field. Multipart entries are ordered text values or payload-store blob references; use format: raw for application/json responses.",
                    "For a public search endpoint, set url to the fixed HTTP(S) address and query_fields to a mapping from query parameter names to input row fields; query holds static parameters. Do not put credentials in URLs or row query values.",
                    "Use headers for fixed Accept, Accept-Language, User-Agent, or X-Requested-With values and header_fields to bind those generic names to row fields. Authentication and Cookie headers are forbidden; configured values are fingerprinted in audit evidence.",
                    "POST bodies are retained in HTTP audit evidence; do not put credentials in them. POST redirects are rejected and POST failures are not automatically retried.",
                    "If you saw Unknown source plugin: web_scrape, use a URL row source first, then add web_scrape as a transform.",
                    "URLs MUST include explicit scheme (http:// or https://). Bare hostnames are rejected by the SSRF guard at fetch time.",
                    "schema is required; use schema: {mode: observed} unless you need fixed/flexible field contracts. For raw HTML, set format to raw, not html.",
                    "web_scrape passes through upstream row fields that the input schema guarantees, and also guarantees content_field, fingerprint_field, fetch_status, fetch_url_final, and fetch_url_final_ip.",
                    "Do not make downstream LLM templates require a URL field unless the upstream source schema or web_scrape schema guarantees that field; if the original URL is required, preserve and guarantee that source field upstream.",
                    "fetch_url_final is a persistence-safe rendering of the final URL (userinfo/fragment stripped, known-sensitive query values fingerprinted, query re-encoded) — use it to identify the fetched resource, not to re-fetch it.",
                    "If validation says a downstream URL field is missing, do not patch web_scrape guaranteed_fields by guess; repair the producer schema, add an explicit mapper, or narrow the downstream template requirements.",
                    "http.abuse_contact and http.scraping_reason are mandatory and recorded in the audit trail — operator must declare them, not the model.",
                    "If the user-facing output should exclude raw scraped content, route the final path through field_mapper with select_only: true before the sink; a sink name or output name is not cleanup.",
                    "A validator-valid direct route from web_scrape or an LLM to the sink is still incomplete when raw scraped-content cleanup is required; insert or restore the final field_mapper before the sink.",
                    "If scraped public internet content flows into an LLM, surface prompt-injection shielding as an important recommendation.",
                    "Recommend an available authorized prompt-injection shield; use azure_prompt_shield only when discovery lists it, or use the deployment's equivalent when available.",
                    "Recommendation is not permission to add a node; do not substitute azure_content_safety; do not insert it automatically unless requested or policy-required.",
                    "If no prompt shield is authorized, make the direct public-content-to-LLM routing reviewable with a pipeline_decision requirement on the LLM node using user_term prompt_injection_shield_recommendation.",
                    "For prompt-injection shielding recommendations, do not add passthrough, placeholder, no-op, or renamed utility nodes to imply protection; recommendation prose is not a graph step.",
                ),
            )
        if issue_code != "web_scrape.content.compact_text":
            return None
        return PluginAssistance(
            plugin_name="web_scrape",
            issue_code="web_scrape.content.compact_text",
            summary=(
                "format='text' with a non-newline text_separator produces a "
                "compact single-line string. Downstream line-oriented "
                "transforms (line_explode) cannot recover line boundaries."
            ),
            suggested_fixes=(
                "Set text_separator: '\\n' to preserve line boundaries.",
                "Or use format: markdown — markdown extraction preserves line-oriented structure.",
            ),
            examples=(
                PluginAssistanceExample(
                    title="Use newline separator with text format",
                    before={"format": "text", "text_separator": " "},
                    after={"format": "text", "text_separator": "\n"},
                ),
                PluginAssistanceExample(
                    title="Switch to markdown format",
                    before={"format": "text", "text_separator": " "},
                    after={"format": "markdown"},
                ),
            ),
        )

    @classmethod
    def get_post_call_hints(
        cls,
        *,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        hints: list[str] = []
        # format=text with whitespace separator → flag the compact_text issue.
        if "format" not in config_snapshot or "text_separator" not in config_snapshot:
            return ()
        if config_snapshot["format"] != "text":
            return ()
        sep = config_snapshot["text_separator"]
        # text_separator is str per WebScrapeConfig schema (Tier-2 type contract)
        if "\n" not in sep:  # type: ignore[operator]
            hints.append(
                "format: 'text' with a non-newline text_separator collapses page lines into one string. "
                "Downstream line_explode cannot recover boundaries. Either set text_separator: '\\n' or switch format: 'markdown'."
            )
        return tuple(hints)

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a deterministic public-IP URL for invariant probing."""
        if self._url_field is not None:
            probe = self._augment_invariant_probe_row(
                probe,
                field_name=self._url_field,
                value="https://93.184.216.34/invariant-probe",
            )
        if self._request_json_field is not None:
            probe = self._augment_invariant_probe_row(probe, field_name=self._request_json_field, value={})
        if self._request_form_field is not None:
            probe = self._augment_invariant_probe_row(probe, field_name=self._request_form_field, value=[{"name": "q", "value": "probe"}])
        if self._request_multipart_field is not None:
            probe = self._augment_invariant_probe_row(
                probe, field_name=self._request_multipart_field, value=[{"name": "q", "value": "probe"}]
            )
        for field_name in self._query_fields.values():
            probe = self._augment_invariant_probe_row(probe, field_name=field_name, value="probe")
        for field_name in self._header_fields.values():
            probe = self._augment_invariant_probe_row(probe, field_name=field_name, value="probe")
        return [probe]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: TransformContext,
    ) -> TransformResult:
        """Drive the real process path with a hermetic no-network fetch seam."""

        class _InvariantPayloadStore:
            def store(self, payload: bytes) -> str:
                return "probe-processed-hash"

        class _InvariantCall:
            request_ref = "probe-request-hash"
            response_ref = "probe-response-hash"

        def _fake_fetch_url(
            safe_request: SSRFSafeRequest,
            probe_ctx: TransformContext,
            request_json: _PostRequestBody | None = None,
            request_form: tuple[tuple[str, str], ...] | None = None,
            request_multipart: tuple[bytes, MultipartMetadata] | None = None,
            request_params: dict[str, str | int | float] | None = None,
            request_headers: dict[str, str] | None = None,
        ) -> tuple[httpx.Response, str, _InvariantCall]:
            del probe_ctx, request_json, request_form, request_multipart, request_headers
            logical_url = str(httpx.Request(self._method, safe_request.original_url, params=request_params).url)
            return (
                httpx.Response(
                    200,
                    text="<html><body><h1>Probe</h1><p>safe</p></body></html>",
                    request=httpx.Request(self._method, safe_request.connection_url, params=request_params),
                ),
                logical_url,
                _InvariantCall(),
            )

        had_payload_store = "_payload_store" in self.__dict__
        original_payload_store: Any = None
        if had_payload_store:
            original_payload_store = self.__dict__["_payload_store"]
        had_fetch_override = "_fetch_url" in self.__dict__
        original_fetch = self._fetch_url
        original_url = self._url
        try:
            if original_url is not None:
                self._url = "https://93.184.216.34/invariant-probe"
            self.__dict__["_payload_store"] = _InvariantPayloadStore()
            self.__dict__["_fetch_url"] = _fake_fetch_url
            return super().execute_forward_invariant_probe(probe_rows, ctx)
        finally:
            self._url = original_url
            if had_payload_store:
                self.__dict__["_payload_store"] = original_payload_store
            else:
                delattr(self, "_payload_store")
            if had_fetch_override:
                self.__dict__["_fetch_url"] = original_fetch
            else:
                delattr(self, "_fetch_url")

    def on_start(self, ctx: LifecycleContext) -> None:
        """Capture infrastructure dependencies at pipeline start."""
        super().on_start(ctx)
        if ctx.landscape is None:
            raise FrameworkBugError("WebScrapeTransform requires landscape — orchestrator must inject it before on_start().")
        if ctx.rate_limit_registry is None:
            raise FrameworkBugError("WebScrapeTransform requires rate_limit_registry — orchestrator must inject it before on_start().")
        if ctx.payload_store is None:
            raise FrameworkBugError("WebScrapeTransform requires payload_store — orchestrator must configure it before on_start().")
        self._recorder = ctx.landscape
        self._payload_store = ctx.payload_store
        self._limiter = ctx.rate_limit_registry
        self._telemetry_emit = ctx.telemetry_emit

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Fetch URL and enrich row with content and fingerprint.

        Args:
            row: Input row (PipelineRow guaranteed by engine)
            ctx: Transform context with token, state_id, run_id

        Returns:
            TransformResult.success() with enriched row, or
            TransformResult.error() for non-retryable failures

        Raises:
            WebScrapeError: For retryable failures (5xx, 429, network)
                Engine RetryManager handles these with exponential backoff
        """
        pagination_started = time.monotonic()
        if self._auth_header is not None:
            return TransformResult.error(
                {
                    "reason": "validation_failed",
                    "error": "authenticated web_scrape is unavailable until response evidence is secret safe",
                }
            )
        request_json: _PostRequestBody | None = None
        request_form: tuple[tuple[str, str], ...] | None = None
        request_multipart: tuple[bytes, MultipartMetadata] | None = None
        request_params: dict[str, str | int | float] = dict(self._query)
        try:
            request_headers = build_request_headers(self._headers, self._header_fields, row)
        except ValueError:
            return TransformResult.error({"reason": "validation_failed", "error": "request headers are invalid"})
        audited_header_names = frozenset(name.casefold() for name in request_headers)
        if self._query_fields:
            values = row.to_dict()
            for name, field_name in self._query_fields.items():
                if field_name not in values:
                    return TransformResult.error({"reason": "validation_failed", "error": f"query row field '{field_name}' is missing"})
                value = values[field_name]
                if type(value) is not str:
                    return TransformResult.error(
                        {"reason": "validation_failed", "error": f"query row field '{field_name}' must contain a string"}
                    )
                request_params[name] = value
        if self._request_json_field is not None:
            values = row.to_dict()
            if self._request_json_field not in values:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST body field '{self._request_json_field}' is missing"}
                )
            try:
                candidate = _PostRequestBody(values[self._request_json_field])
            except ValueError:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST body field '{self._request_json_field}' must contain a JSON object"}
                )
            try:
                encoded_body = httpx.Request("POST", "https://example.invalid/", json=candidate).content
            except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST body field '{self._request_json_field}' cannot be JSON encoded"}
                )
            if len(encoded_body) > self._max_request_body_bytes:
                return TransformResult.error(
                    {
                        "reason": "validation_failed",
                        "error": f"POST body exceeds max_request_body_bytes {self._max_request_body_bytes}",
                        "body_size": len(encoded_body),
                        "max_body_bytes": self._max_request_body_bytes,
                    }
                )
            request_json = candidate
        if self._request_form_field is not None:
            values = row.to_dict()
            if self._request_form_field not in values:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST form field '{self._request_form_field}' is missing"}
                )
            try:
                request_form = _parse_form_fields(values[self._request_form_field])
                encoded_form = encode_urlencoded_form(request_form)
            except ValueError:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST form field '{self._request_form_field}' is invalid"}
                )
            if len(encoded_form) > self._max_request_body_bytes:
                return TransformResult.error(
                    {
                        "reason": "validation_failed",
                        "error": f"POST form exceeds max_request_body_bytes {self._max_request_body_bytes}",
                        "body_size": len(encoded_form),
                        "max_body_bytes": self._max_request_body_bytes,
                    }
                )
        if self._request_multipart_field is not None:
            values = row.to_dict()
            if self._request_multipart_field not in values:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST multipart field '{self._request_multipart_field}' is missing"}
                )
            try:
                parts = _parse_multipart_parts(values[self._request_multipart_field])
            except ValueError:
                return TransformResult.error(
                    {"reason": "validation_failed", "error": f"POST multipart field '{self._request_multipart_field}' is invalid"}
                )
            try:
                remaining_file_bytes = self._max_request_body_bytes - multipart_min_body_size(
                    parts, max_body_bytes=self._max_request_body_bytes
                )
            except ValueError:
                return TransformResult.error(
                    {
                        "reason": "validation_failed",
                        "error": f"POST multipart exceeds max_request_body_bytes {self._max_request_body_bytes}",
                    }
                )
            blobs: dict[str, bytes] = {}
            for part in parts:
                if part.blob_ref is None:
                    continue
                if part.blob_ref not in blobs:
                    try:
                        blob_content = self._payload_store.retrieve_bounded(part.blob_ref, max_bytes=remaining_file_bytes)
                    except PayloadNotFoundError:
                        return TransformResult.error({"reason": "blob_not_found", "blob_ref": part.blob_ref})
                    if blob_content is None:
                        return TransformResult.error(
                            {
                                "reason": "validation_failed",
                                "error": f"POST multipart exceeds max_request_body_bytes {self._max_request_body_bytes}",
                            }
                        )
                    blobs[part.blob_ref] = blob_content
                if len(blobs[part.blob_ref]) > remaining_file_bytes:
                    return TransformResult.error(
                        {
                            "reason": "validation_failed",
                            "error": f"POST multipart exceeds max_request_body_bytes {self._max_request_body_bytes}",
                        }
                    )
                remaining_file_bytes -= len(blobs[part.blob_ref])
            try:
                request_multipart = encode_multipart_form(parts, blobs, max_body_bytes=self._max_request_body_bytes)
            except ValueError:
                return TransformResult.error(
                    {
                        "reason": "validation_failed",
                        "error": f"POST multipart is invalid or exceeds max_request_body_bytes {self._max_request_body_bytes}",
                    }
                )

        # Validate URL and pin resolved IP (SSRF prevention with DNS rebinding defense)
        try:
            if self._url is not None:
                url = self._url
            elif self._url_field is not None:
                url = row[self._url_field]
            else:
                raise FrameworkBugError("web_scrape has no configured URL source")
            if type(url) is not str:
                raise TypeError("URL field must be a string")
            validate_allowed_http_origin(url, self._request_allowed_origins)
            if request_params:
                try:
                    query_url = httpx.Request(self._method, url, params=request_params).url
                    encoded_url_size = len(str(query_url).encode("utf-8"))
                except (TypeError, ValueError, UnicodeError):
                    return TransformResult.error({"reason": "validation_failed", "error": "query parameters cannot be URL encoded"})
                if encoded_url_size > 8192:
                    return TransformResult.error({"reason": "validation_failed", "error": "URL with query parameters exceeds 8192 bytes"})
            session = ctx.call_mode_session
            if session is not None and session.mode is RunMode.REPLAY:
                archived = session.replay_ssrf_request(
                    original_url=url,
                    audited_url=fingerprint_url(url),
                    call_type=CallType.HTTP,
                    current_state_id=ctx.state_id,
                    current_operation_id=None,
                )
                safe_request = validate_archived_ssrf_request(url, archived, allowed_ranges=self._allowed_ranges)
            elif session is not None and session.mode is RunMode.VERIFY:
                archived = session.replay_ssrf_request(
                    original_url=url,
                    audited_url=fingerprint_url(url),
                    call_type=CallType.HTTP,
                    current_state_id=ctx.state_id,
                    current_operation_id=None,
                )
                archived_safe = validate_archived_ssrf_request(url, archived, allowed_ranges=self._allowed_ranges)
                pre_dns_request = HTTPCallRequest(
                    method=self._method,
                    url=fingerprint_url(url),
                    headers=fingerprint_headers(
                        {
                            "X-Abuse-Contact": self._abuse_contact,
                            "X-Scraping-Reason": self._scraping_reason,
                            "Host": archived_safe.host_header,
                            **request_headers,
                            **(dict((self._auth_header,)) if self._auth_header is not None else {}),
                            **({"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"} if request_form is not None else {}),
                            **({"Content-Type": request_multipart[1].content_type} if request_multipart is not None else {}),
                        },
                        force_fingerprint_names=audited_header_names
                        | (frozenset({self._auth_header[0].casefold()}) if self._auth_header is not None else frozenset()),
                    ),
                    params=fingerprint_params(request_params) if request_params else None,
                    json=request_json,
                    form=request_form,
                    multipart=request_multipart[1] if request_multipart is not None else None,
                )
                session.preflight_verify_http_request(
                    request_data=pre_dns_request.to_dict(),
                    current_state_id=ctx.state_id,
                    current_operation_id=None,
                )
                safe_request = validate_url_for_ssrf(url, allowed_ranges=self._allowed_ranges)
            else:
                safe_request = validate_url_for_ssrf(url, allowed_ranges=self._allowed_ranges)
        # Missing row fields, security violations, DNS failures, and invalid
        # URL value types are row-level validation failures, not retries.
        except SSRFBlockedError as e:
            return TransformResult.error(row_url_policy_refusal(e))
        except SSRFNetworkError as e:
            return TransformResult.error(row_url_policy_refusal(e))
        except TypeError as e:
            return TransformResult.error(row_url_value_refusal(URL_NOT_A_STRING, e))
        except KeyError as e:
            return TransformResult.error(row_url_value_refusal(URL_FIELD_MISSING, e))

        # Fetch URL using pinned IP (prevents DNS rebinding between validation and fetch)
        try:
            if request_headers or self._auth_header is not None:
                response, final_hostname_url, call = self._fetch_url(
                    safe_request,
                    ctx,
                    request_json,
                    request_form=request_form,
                    request_multipart=request_multipart,
                    request_params=request_params or None,
                    request_headers=request_headers,
                )
            elif request_multipart is not None:
                if request_params:
                    response, final_hostname_url, call = self._fetch_url(
                        safe_request, ctx, request_multipart=request_multipart, request_params=request_params
                    )
                else:
                    response, final_hostname_url, call = self._fetch_url(safe_request, ctx, request_multipart=request_multipart)
            elif request_form is not None:
                if request_params:
                    response, final_hostname_url, call = self._fetch_url(
                        safe_request, ctx, request_form=request_form, request_params=request_params
                    )
                else:
                    response, final_hostname_url, call = self._fetch_url(safe_request, ctx, request_form=request_form)
            elif self._method == "POST":
                if request_params:
                    response, final_hostname_url, call = self._fetch_url(safe_request, ctx, request_json, request_params=request_params)
                else:
                    response, final_hostname_url, call = self._fetch_url(safe_request, ctx, request_json)
            elif request_params:
                response, final_hostname_url, call = self._fetch_url(safe_request, ctx, request_params=request_params)
            else:
                response, final_hostname_url, call = self._fetch_url(safe_request, ctx)
            final_resolved_ip = _final_response_ip(response)
        except BodyTooLargeError as e:
            # Rebuild the message from structured fields — str(e) carries the
            # underlying HTTP-layer text, which embeds the raw hop URL.
            return TransformResult.error(
                {
                    "reason": "body_too_large",
                    "error": f"response body {e.body_size} bytes exceeds max_body_bytes {e.max_body_bytes}",
                    "body_size": e.body_size,
                    "max_body_bytes": e.max_body_bytes,
                }
            )
        except HTTPResponseEncodingLimitError as e:
            return TransformResult.error({"reason": "body_too_large", "error": "response encoding limit exceeded", "error_type": e.reason})
        except WebScrapeError as e:
            if e.retryable and self._method == "GET":
                # Re-raise retryable errors for engine RetryManager
                raise
            # Non-retryable errors return error result
            return TransformResult.error(
                {
                    "reason": "api_error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )

        # The shared client has already capped streamed decoded bytes. This
        # admission also checks injected/replayed responses before extraction.
        try:
            admitted = admit_response_content(
                response,
                mode=self._response_mode,
                page_format=self._format,
                accepted_mime_types=self._accepted_mime_types,
                charset_policy=self._charset_policy,
                max_decoded_bytes=self._max_decoded_body_bytes,
            )
        except ResponseContentError as e:
            reason: TransformErrorReason = {"reason": e.reason, "error": str(e), "error_type": e.code}
            if e.reason == "non_text_content_type":
                reason["content_type"] = response.headers["content-type"] if "content-type" in response.headers else None
            if e.reason == "body_too_large":
                reason["body_size"] = len(response.content)
                reason["max_body_bytes"] = self._max_decoded_body_bytes
            return TransformResult.error(reason)

        # Extract content -- response.text is Tier 3 (external data), validate at boundary
        try:
            content = extract_content(
                admitted.text,
                format=self._format,
                strip_elements=self._strip_elements,
                text_separator=self._text_separator,
            )
        except (ValueError, UnicodeDecodeError, UnicodeEncodeError, RuntimeError) as e:
            return TransformResult.error(
                {
                    "reason": "content_extraction_failed",
                    "error": str(e),
                    "error_type": type(e).__name__,
                }
            )

        body_size = len(response.content)
        records: list[dict[str, str | list[str] | None]] | None = None
        record_provenance: list[dict[str, dict[str, object]]] | None = None
        if self._records is not None:
            try:
                records, record_provenance = self._extract_record_rows(admitted.text, admitted.content_type, final_hostname_url)
            except (ValueError, UnicodeError) as e:
                return TransformResult.error({"reason": "content_extraction_failed", "error": str(e), "error_type": type(e).__name__})

        pagination_pages: list[PageProvenance] = []
        pagination_stop_reason: str | None = None
        if self._pagination is not None:
            if call.request_ref is None or call.response_ref is None:
                raise FrameworkBugError("AuditedHTTPClient returned a Call with no request_ref/response_ref")
            if body_size > self._pagination.max_total_body_bytes:
                return TransformResult.error(
                    {"reason": "pagination_limit_exceeded", "max_body_bytes": self._pagination.max_total_body_bytes}
                )
            if len(self._aggregate_page_contents([content])) > self._pagination.max_total_content_chars or (
                records is not None and len(records) > self._pagination.max_total_records
            ):
                return TransformResult.error({"reason": "pagination_limit_exceeded", "error": "aggregate output limit exceeded"})
            initial_request_url = str(httpx.Request("GET", url, params=request_params or None).url)
            page_contents = [content]
            page_records = list(records) if records is not None else None
            seen_urls = {initial_request_url, final_hostname_url}
            total_body_bytes = body_size
            next_source = self._pagination.mode
            pagination_pages.append(
                PageProvenance(
                    number=1,
                    request_url=fingerprint_url(initial_request_url),
                    final_url=fingerprint_url(final_hostname_url),
                    request_ref=call.request_ref,
                    response_ref=call.response_ref,
                    body_bytes=body_size,
                    next_source=next_source,
                )
            )
            while True:
                try:
                    next_url = discover_next_url(response, final_hostname_url, self._pagination)
                except (ValueError, UnicodeError) as exc:
                    return TransformResult.error({"reason": "validation_failed", "error": str(exc)})
                if time.monotonic() - pagination_started > self._pagination.max_elapsed_seconds:
                    return TransformResult.error({"reason": "pagination_limit_exceeded", "error": "elapsed time limit exceeded"})
                if next_url is not None:
                    query_string = urlsplit(next_url).query
                    if len(query_string) > MAX_AUDIT_QUERY_CHARS:
                        return TransformResult.error({"reason": "validation_failed", "error": "next URL query exceeds audit bounds"})
                    try:
                        query_pairs = parse_qsl(query_string, keep_blank_values=True, max_num_fields=MAX_AUDIT_QUERY_FIELDS)
                    except ValueError:
                        return TransformResult.error({"reason": "validation_failed", "error": "next URL has too many query fields"})
                    if any(is_disallowed_pagination_query_param(name) for name, _value in query_pairs):
                        return TransformResult.error(
                            {"reason": "validation_failed", "error": "next URL has a credential or opaque cursor query name"}
                        )
                    try:
                        validate_configured_url_for_ssrf(next_url, allowed_ranges=self._allowed_ranges)
                    except SSRFBlockedError as exc:
                        return TransformResult.error(row_url_policy_refusal(exc))
                    # The audited HTTP transport can replay only a logical URL
                    # whose persisted form is byte-identical. Canonicalize
                    # ordinary query encoding before SSRF admission and fetch.
                    next_url = fingerprint_url(next_url)
                    pagination_pages[-1] = replace(pagination_pages[-1], selected_next_url=next_url)
                if next_url is None:
                    pagination_stop_reason = "no_next_link"
                    break
                if next_url in seen_urls:
                    pagination_stop_reason = "cycle"
                    break
                if len(pagination_pages) >= self._pagination.max_pages:
                    pagination_stop_reason = "max_pages"
                    break
                if time.monotonic() - pagination_started > self._pagination.max_elapsed_seconds:
                    return TransformResult.error({"reason": "pagination_limit_exceeded", "error": "elapsed time limit exceeded"})
                try:
                    safe_next = self._validate_pagination_url(next_url, ctx, request_headers=request_headers)
                except (SSRFBlockedError, SSRFNetworkError) as exc:
                    return TransformResult.error(row_url_policy_refusal(exc))
                try:
                    if request_headers:
                        response, final_hostname_url, call = self._fetch_url(safe_next, ctx, request_headers=request_headers)
                    else:
                        response, final_hostname_url, call = self._fetch_url(safe_next, ctx)
                except BodyTooLargeError as exc:
                    return TransformResult.error(
                        {"reason": "body_too_large", "body_size": exc.body_size, "max_body_bytes": exc.max_body_bytes}
                    )
                except HTTPResponseEncodingLimitError as exc:
                    return TransformResult.error(
                        {"reason": "body_too_large", "error": "response encoding limit exceeded", "error_type": exc.reason}
                    )
                except WebScrapeError as exc:
                    if exc.retryable:
                        raise
                    return TransformResult.error({"reason": "api_error", "error": str(exc), "error_type": type(exc).__name__})
                if call.request_ref is None or call.response_ref is None:
                    raise FrameworkBugError("AuditedHTTPClient returned a pagination Call with no request_ref/response_ref")
                body_size = len(response.content)
                total_body_bytes += body_size
                if total_body_bytes > self._pagination.max_total_body_bytes:
                    return TransformResult.error(
                        {"reason": "pagination_limit_exceeded", "max_body_bytes": self._pagination.max_total_body_bytes}
                    )
                if time.monotonic() - pagination_started > self._pagination.max_elapsed_seconds:
                    return TransformResult.error({"reason": "pagination_limit_exceeded", "error": "elapsed time limit exceeded"})
                try:
                    admitted = admit_response_content(
                        response,
                        mode=self._response_mode,
                        page_format=self._format,
                        accepted_mime_types=self._accepted_mime_types,
                        charset_policy=self._charset_policy,
                        max_decoded_bytes=self._max_decoded_body_bytes,
                    )
                except ResponseContentError as exc:
                    return TransformResult.error({"reason": exc.reason, "error": str(exc), "error_type": exc.code})
                try:
                    next_content = extract_content(
                        admitted.text,
                        format=self._format,
                        strip_elements=self._strip_elements,
                        text_separator=self._text_separator,
                    )
                    if self._records is not None:
                        if page_records is None:
                            raise FrameworkBugError("Record accumulator missing")
                        next_records, next_provenance = self._extract_record_rows(admitted.text, admitted.content_type, final_hostname_url)
                        page_records.extend(next_records)
                        if self._records.provenance_field is not None:
                            if record_provenance is None or next_provenance is None:
                                raise FrameworkBugError("Record provenance accumulator missing")
                            record_provenance.extend(next_provenance)
                except (ValueError, UnicodeError, RuntimeError) as exc:
                    return TransformResult.error(
                        {"reason": "content_extraction_failed", "error": str(exc), "error_type": type(exc).__name__}
                    )
                page_contents.append(next_content)
                if len(self._aggregate_page_contents(page_contents)) > self._pagination.max_total_content_chars or (
                    page_records is not None and len(page_records) > self._pagination.max_total_records
                ):
                    return TransformResult.error({"reason": "pagination_limit_exceeded", "error": "aggregate output limit exceeded"})
                pagination_pages.append(
                    PageProvenance(
                        number=len(pagination_pages) + 1,
                        request_url=fingerprint_url(next_url),
                        final_url=fingerprint_url(final_hostname_url),
                        request_ref=call.request_ref,
                        response_ref=call.response_ref,
                        body_bytes=body_size,
                        next_source=next_source,
                    )
                )
                seen_urls.update((next_url, final_hostname_url))
            content = self._aggregate_page_contents(page_contents)
            records = page_records
            final_resolved_ip = _final_response_ip(response)

        # Compute fingerprint
        fingerprint = compute_fingerprint(content, mode=self._fingerprint_mode)

        # Field collision check already done before fetch — no need to re-check here.

        # Hashes from audit trail — request and response blobs are already stored
        # by AuditedHTTPClient via recorder.record_call()
        if call.request_ref is None or call.response_ref is None:
            raise FrameworkBugError(
                "AuditedHTTPClient returned a Call with no request_ref/response_ref — "
                "PayloadStore must be configured for "
                "hash-based audit provenance in WebScrapeTransform."
            )
        request_hash = call.request_ref
        response_raw_hash = call.response_ref

        # Store processed content via PayloadStore (transform-produced artifact).
        # payload_store is guaranteed non-None by on_start() validation.
        response_processed_hash = self._payload_store.store(content.encode())

        # Enrich row with scraped data — operational fields only
        # Use explicit to_dict() conversion (PipelineRow guaranteed by engine)
        output = row.to_dict()
        output[self._content_field] = content
        output[self._fingerprint_field] = fingerprint
        if records is not None and self._records is not None:
            output[self._records.field] = records
            if record_provenance is not None and self._records.provenance_field is not None:
                output[self._records.provenance_field] = record_provenance
        output["fetch_status"] = response.status_code
        # Redirects are attacker-influenced and this value is PERSISTED — onto the
        # row and through it into the audit trail — so userinfo and the fragment
        # are stripped and known-sensitive query values fingerprinted first.
        # Matches blob_fetch (7e8840bad); a query-free URL passes through
        # unchanged, but a query string is parse/re-encode normalised.
        output["fetch_url_final"] = fingerprint_url(final_hostname_url)
        output["fetch_url_final_ip"] = final_resolved_ip

        # Propagate contract so FIXED schemas can access fields added during enrichment
        output_contract = narrow_contract_to_output(
            input_contract=row.contract,
            output_row=output,
        )
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "enriched",
                "fields_added": [
                    self._content_field,
                    self._fingerprint_field,
                    *([self._records.field] if self._records is not None else []),
                    *([self._records.provenance_field] if self._records is not None and self._records.provenance_field is not None else []),
                ],
                "metadata": {
                    "fetch_request_hash": request_hash,
                    "fetch_response_raw_hash": response_raw_hash,
                    "fetch_response_processed_hash": response_processed_hash,
                    "fetch_content_type": admitted.content_type,
                    "fetch_charset": admitted.charset,
                    "fetch_decoded_body_bytes": len(response.content),
                    **(
                        {
                            "pagination_pages": [page.to_audit() for page in pagination_pages],
                            "pagination_stop_reason": pagination_stop_reason,
                            "pagination_total_body_bytes": sum(page.body_bytes for page in pagination_pages),
                        }
                        if self._pagination is not None
                        else {}
                    ),
                },
            },
        )

    def _aggregate_page_contents(self, pages: list[str]) -> str:
        """Keep strict document modes syntactically valid after pagination."""
        if self._response_mode == "json":
            return "[" + ",".join(pages) + "]"
        if self._response_mode == "xml":
            return "<pages>" + "".join("<page>" + escape_xml_text(page) + "</page>" for page in pages) + "</pages>"
        separator = self._text_separator if self._format == "text" else "\n"
        return separator.join(pages)

    def _extract_record_rows(
        self, text: str, content_type: str, source_url: str
    ) -> tuple[list[dict[str, str | list[str] | None]], list[dict[str, dict[str, object]]] | None]:
        """Apply the configured extraction contract to every admitted page."""
        config = self._records
        if isinstance(config, JSONRecordsConfig):
            extracted_json = extract_json_records_with_provenance(text, config, source_url=fingerprint_url(source_url))
            provenance = (
                [{field: asdict(evidence) for field, evidence in row.items()} for row in extracted_json.provenance]
                if config.provenance_field is not None
                else None
            )
            return extracted_json.records, provenance
        if not isinstance(config, CSSRecordsConfig):
            raise FrameworkBugError("Record extraction called without a configured record contract")
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("CSS records require an HTML response")

        def resolve_link(href: str) -> str:
            return resolve_discovered_href(
                href, base_url=source_url, allowed_origins=self._allowed_origins, allowed_ranges=self._allowed_ranges
            )

        if config.provenance_field is not None:
            extracted_css = extract_css_records_with_provenance(
                text, config, self._strip_elements, source_url=fingerprint_url(source_url), url_resolver=resolve_link
            )
            return extracted_css.to_record_rows(), [
                {field: dict(evidence) for field, evidence in row.items()} for row in extracted_css.to_provenance_rows()
            ]
        return extract_css_records(text, config, self._strip_elements, url_resolver=resolve_link), None

    def _validate_pagination_url(self, url: str, ctx: TransformContext, *, request_headers: dict[str, str]) -> SSRFSafeRequest:
        """Admit every discovered GET through the same origin and SSRF gates."""
        validate_allowed_http_origin(url, self._allowed_origins)
        session = ctx.call_mode_session
        if session is not None and session.mode in (RunMode.REPLAY, RunMode.VERIFY):
            archived = session.replay_ssrf_request(
                original_url=url,
                audited_url=fingerprint_url(url),
                call_type=CallType.HTTP,
                current_state_id=ctx.state_id,
                current_operation_id=None,
            )
            archived_safe = validate_archived_ssrf_request(url, archived, allowed_ranges=self._allowed_ranges)
            if session.mode is RunMode.REPLAY:
                return archived_safe
            pre_dns_request = HTTPCallRequest(
                method="GET",
                url=fingerprint_url(url),
                headers=fingerprint_headers(
                    {
                        "X-Abuse-Contact": self._abuse_contact,
                        "X-Scraping-Reason": self._scraping_reason,
                        "Host": archived_safe.host_header,
                        **request_headers,
                    },
                    force_fingerprint_names=frozenset(name.casefold() for name in request_headers),
                ),
            )
            session.preflight_verify_http_request(
                request_data=pre_dns_request.to_dict(),
                current_state_id=ctx.state_id,
                current_operation_id=None,
            )
        return validate_url_for_ssrf(url, allowed_ranges=self._allowed_ranges)

    def _fetch_url(
        self,
        safe_request: SSRFSafeRequest,
        ctx: TransformContext,
        request_json: _PostRequestBody | None = None,
        request_form: tuple[tuple[str, str], ...] | None = None,
        request_multipart: tuple[bytes, MultipartMetadata] | None = None,
        request_params: dict[str, str | int | float] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> tuple[httpx.Response, str, Call]:
        """Fetch URL using SSRF-safe IP pinning with audit recording.

        Args:
            safe_request: Pre-validated SSRFSafeRequest with pinned IP
            ctx: Plugin context

        Returns:
            Tuple of (httpx.Response, final hostname URL as string, Call).
            The hostname URL is the logical URL after redirects — distinct
            from response.url which is IP-based due to SSRF pinning.
            The Call contains request_ref and response_ref blob hashes.

        Raises:
            WebScrapeError: For retryable or non-retryable failures
        """
        # Infrastructure captured in on_start()
        if ctx.state_id is None:
            raise FrameworkBugError("ctx.state_id not set by executor — executor must set state_id before calling process().")
        # Every message raised below can be persisted via TransformResult.error()
        # or the retry path (node_states.error_json), and the underlying
        # httpx/SSRF exception text embeds the URL — row data that stays in the
        # row carrier and the recorded call — so no message names the URL and
        # none repeats str(e).
        limiter = self._limiter.get_limiter("web_scrape")

        # Create audited client (records to Landscape)
        client = AuditedHTTPClient(
            member_token=ctx.require_member_token(),
            work_item=ctx.require_work_item(),
            execution=self._recorder,
            state_id=ctx.state_id,
            run_id=ctx.run_id,
            telemetry_emit=self._telemetry_emit,
            timeout=self._timeout,
            limiter=limiter,
            token_id=ctx.token.token_id if ctx.token is not None else None,
            max_response_body_bytes=self._max_decoded_body_bytes,
            max_encoded_response_bytes=self._max_encoded_body_bytes,
            max_decompression_ratio=self._max_decompression_ratio,
            call_mode_session=ctx.call_mode_session,
        )

        # Add responsible scraping headers
        headers = {
            "X-Abuse-Contact": self._abuse_contact,
            "X-Scraping-Reason": self._scraping_reason,
            **(request_headers or {}),
            **(dict((self._auth_header,)) if self._auth_header is not None else {}),
        }
        fingerprinted_header_names = frozenset(name.casefold() for name in (request_headers or {})) | (
            frozenset({self._auth_header[0].casefold()}) if self._auth_header is not None else frozenset()
        )

        try:
            if self._method == "POST":
                response, final_hostname_url, call = client.request_ssrf_safe(
                    "POST",
                    safe_request,
                    headers=headers,
                    fingerprinted_header_names=fingerprinted_header_names,
                    json=request_json,
                    form=request_form,
                    multipart_body=request_multipart[0] if request_multipart is not None else None,
                    multipart_metadata=request_multipart[1] if request_multipart is not None else None,
                    params=request_params or None,
                    follow_redirects=False,
                    allowed_ranges=self._allowed_ranges,
                    allowed_origins=self._request_allowed_origins,
                )
            else:
                if request_params:
                    response, final_hostname_url, call = client.request_ssrf_safe(
                        "GET",
                        safe_request,
                        headers=headers,
                        fingerprinted_header_names=fingerprinted_header_names,
                        params=request_params,
                        follow_redirects=True,
                        allowed_ranges=self._allowed_ranges,
                        allowed_origins=self._request_allowed_origins,
                    )
                else:
                    response, final_hostname_url, call = client.get_ssrf_safe(
                        safe_request,
                        headers=headers,
                        fingerprinted_header_names=fingerprinted_header_names,
                        follow_redirects=True,
                        allowed_ranges=self._allowed_ranges,
                        allowed_origins=self._request_allowed_origins,
                    )

            # Check status code and raise appropriate errors
            if response.status_code == 404:
                raise NotFoundError("HTTP 404")
            elif response.status_code == 403:
                raise ForbiddenError("HTTP 403")
            elif response.status_code == 401:
                raise UnauthorizedError("HTTP 401")
            elif response.status_code == 429:
                raise RateLimitError("HTTP 429")
            elif 500 <= response.status_code < 600:
                raise ServerError(f"HTTP {response.status_code}")
            elif 300 <= response.status_code < 400:
                # POST does not follow redirects; GET can still reach this arm
                # for a response without a usable Location header.
                if response.status_code == 304:
                    raise InvalidURLError("HTTP 304 requires archived conditional content; no cache is configured")
                if self._method == "POST":
                    raise InvalidURLError(f"Unfollowed POST redirect HTTP {response.status_code}")
                raise InvalidURLError(f"Unresolved redirect HTTP {response.status_code} (missing or empty Location header)")
            elif 400 <= response.status_code < 500:
                # Catch-all for unenumerated 4xx codes (400, 402, 405, 406, 408,
                # 410, 418, 451, ...). Without this arm the response would be
                # returned and process() would fingerprint the error-page body as
                # if it were real content -- corrupting change-detection (B3.9).
                # 408 Request Timeout is retryable (transient server overload);
                # all other unenumerated 4xx codes are non-retryable client errors.
                retryable = response.status_code == 408
                raise ClientError(f"HTTP {response.status_code}", retryable=retryable)

            return response, final_hostname_url, call

        except httpx.TimeoutException as e:
            raise NetworkError("Timeout fetching the row's URL") from e
        except httpx.ConnectError as e:
            raise NetworkError("Connection error fetching the row's URL") from e
        except HTTPResponseBodyTooLargeError as e:
            # str(e) embeds the raw hop URL — rebuild from structured fields.
            raise BodyTooLargeError(
                f"response body {e.body_size} bytes exceeds max_body_bytes {e.max_body_bytes}",
                body_size=e.body_size,
                max_body_bytes=e.max_body_bytes,
            ) from e
        except SSRFBlockedError as e:
            # Redirect hop resolved to a blocked IP — non-retryable security violation
            from elspeth.plugins.transforms.web_scrape_errors import SSRFBlockedError as WSSRFBlockedError

            raise WSSRFBlockedError("SSRF blocked during a redirect") from e
        except SSRFNetworkError as e:
            # DNS resolution failed during redirect hop
            raise NetworkError("DNS resolution failed during a redirect") from e
        except httpx.TooManyRedirects as e:
            raise InvalidURLError("Too many redirects") from e
        except httpx.RequestError as e:
            raise NetworkError(f"HTTP request error fetching the row's URL ({type(e).__name__})") from e
        finally:
            client.close()

    def close(self) -> None:
        """Release resources."""
        pass
