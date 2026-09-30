"""Credential-material classification for ELSPETH control-plane boundaries.

This detector defines a finite, versioned product boundary.  It recognizes
declared credential fields, a closed set of credential token shapes, private
keys, and credentials embedded in HTTP URLs.  It does not decode arbitrary
base64, hex, compressed, encrypted, or cross-field content and is not a
general-purpose DLP system.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import ParseResult, parse_qs, urlparse

from elspeth.contracts.url import SENSITIVE_PARAMS

CREDENTIAL_DETECTOR_VERSION: Final[str] = "1"
REDACTED_CREDENTIAL_TEXT: Final[str] = "<redacted-secret>"
CREDENTIAL_REFUSAL_DETAIL: Final[str] = (
    "This control content appears to contain a credential. Store the value through the secret service and use a secret reference."
)

CredentialCategory = Literal[
    "credential_field",
    "token_shape",
    "private_key",
    "credential_url",
    "sensitive_url_parameter",
    "traversal_limit",
    "unsupported_value",
]
CredentialLocation = Literal["root", "mapping_key", "mapping_value", "sequence_item", "traversal_boundary"]

SECRET_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "aws_access_key_id",
        "authorization",
        "auth_cookie",
        "auth_token",
        "bearer_token",
        "client_secret",
        "connection_string",
        "conn_string",
        "credential",
        "credentials",
        "id_token",
        "password",
        "passwd",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "sas_token",
        "secret",
        "secret_key",
        "session_token",
        "token",
        "x_api_key",
        "x_auth_token",
    }
)
SECRET_FIELD_SUFFIXES: Final[tuple[str, ...]] = (
    "_secret",
    "_key",
    "_token",
    "_password",
    "_credential",
    "_connection_string",
)
STRUCTURAL_FIELD_EXEMPTIONS: Final[frozenset[str]] = frozenset({"data_key", "alternate_key"})
_NORMALIZED_SECRET_FIELDS = frozenset(name.replace("_", "").replace("-", "") for name in SECRET_FIELD_NAMES)
_NORMALIZED_STRUCTURAL_FIELD_EXEMPTIONS = frozenset(name.replace("_", "").replace("-", "") for name in STRUCTURAL_FIELD_EXEMPTIONS)
_EXACT_ENV_VAR_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_REFERENCE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SECRET_REFERENCE_NAME_MAX_BYTES = 256
_URL_PARSE_ERROR: Final[Literal["invalid-url"]] = "invalid-url"

CREDENTIAL_PATTERN_SPECS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE)),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}")),
    ("openrouter_key", re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}")),
    ("github_fine_grained_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"gh[opsr]_[A-Za-z0-9]{36,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack_token", re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}")),
    ("slack_app_token", re.compile(r"xapp-[A-Za-z0-9-]{10,}")),
    ("slack_workspace_token", re.compile(r"xoxe-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")),
    ("azure_storage_key", re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{86}==(?=$|[^A-Za-z0-9+/=])")),
    ("sas_signature", re.compile(r"sig=[A-Za-z0-9%/+=]{20,}", re.IGNORECASE)),
    ("connection_password", re.compile(r"(?:password|pwd)=[^;\s]+", re.IGNORECASE)),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
            r"auth[_-]?token|id[_-]?token|bearer[_-]?token|client[_-]?secret|"
            r"private[_-]?key|secret[_-]?key|connection[_-]?string|authorization|"
            r"password|passwd|pwd|credentials?)[\"']?[ \t]*[:=][ \t]*['\"]?"
            r"(?!\$\{[A-Za-z_][A-Za-z0-9_]*\}(?:['\"]?(?:\s|[,;)}\]]|$)))"
            r"[^'\"\s,;(){}\[\]]+",
            re.IGNORECASE,
        ),
    ),
)
_PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_HTTP_URL_CANDIDATE_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_CREDENTIAL_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"postgres(?:ql)?://[^:/\s]+:[^@/\s]+@"),
    re.compile(r"mysql://[^:/\s]+:[^@/\s]+@"),  # secret-scan: allow-this-line
    re.compile(r"mongodb(?:\+srv)?://[^:/\s]+:[^@/\s]+@"),
)


@dataclass(frozen=True, slots=True)
class CredentialTraversalPolicy:
    """Explicit resource and reference policy for one detector traversal."""

    env_ref_names: Collection[str] = frozenset()
    additional_credential_fields: Collection[str] = frozenset()
    max_depth: int = 32
    max_nodes: int = 4096
    max_width: int = 512
    max_string_bytes: int = 262_144


@dataclass(frozen=True, slots=True)
class CredentialMaterialFinding:
    """Value-free evidence returned by the credential detector."""

    category: CredentialCategory
    location: CredentialLocation
    detector_version: str = CREDENTIAL_DETECTOR_VERSION


DEFAULT_CREDENTIAL_TRAVERSAL_POLICY: Final[CredentialTraversalPolicy] = CredentialTraversalPolicy()


def is_secret_field(field_name: str, additional_fields: Collection[str] = frozenset()) -> bool:
    """Return whether ``field_name`` is in the closed credential vocabulary."""
    normalized = field_name.lower()
    compact = normalized.replace("_", "").replace("-", "")
    if compact in _NORMALIZED_STRUCTURAL_FIELD_EXEMPTIONS:
        return False
    if normalized in {field.lower() for field in additional_fields}:
        return True
    separator_normalized = normalized.replace("-", "_")
    return compact in _NORMALIZED_SECRET_FIELDS or separator_normalized.endswith(SECRET_FIELD_SUFFIXES)


def find_credential_material(
    value: Any,
    policy: CredentialTraversalPolicy = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
) -> CredentialMaterialFinding | None:
    """Return the first value-free credential finding in a bounded tree.

    Mappings, lists, and tuples form the admitted control-tree vocabulary.
    Exact managed-reference markers and declared exact environment references
    are allowed only as complete values under credential fields.
    """
    state = _TraversalState(policy=policy, active_ids=set())
    return _find(value, state=state, depth=0, location="root", credential_parent=False)


@dataclass(slots=True)
class _TraversalState:
    policy: CredentialTraversalPolicy
    active_ids: set[int]
    nodes: int = 0


def _find(
    value: Any,
    *,
    state: _TraversalState,
    depth: int,
    location: CredentialLocation,
    credential_parent: bool,
) -> CredentialMaterialFinding | None:
    state.nodes += 1
    if depth > state.policy.max_depth or state.nodes > state.policy.max_nodes:
        return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
    if credential_parent:
        if _is_empty(value):
            return None
        reference_finding = _allowed_reference_finding(value, state.policy)
        if reference_finding is not False:
            return reference_finding
        return CredentialMaterialFinding("credential_field", location)
    if type(value) is str:
        if len(value.encode("utf-8", errors="replace")) > state.policy.max_string_bytes:
            return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
        category = _classify_string(value)
        return CredentialMaterialFinding(category, location) if category is not None else None
    if value is None or type(value) in (bool, int, float):
        return None
    if type(value) in (dict, MappingProxyType):
        if len(value) > state.policy.max_width:
            return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
        object_id = id(value)
        if object_id in state.active_ids:
            return CredentialMaterialFinding("unsupported_value", "traversal_boundary")
        state.active_ids.add(object_id)
        try:
            for key, child in value.items():
                if type(key) is not str:
                    return CredentialMaterialFinding("unsupported_value", "mapping_key")
                if len(key.encode("utf-8", errors="replace")) > state.policy.max_string_bytes:
                    return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
                key_category = _classify_string(key)
                if key_category is not None:
                    return CredentialMaterialFinding(key_category, "mapping_key")
                finding = _find(
                    child,
                    state=state,
                    depth=depth + 1,
                    location="mapping_value",
                    credential_parent=is_secret_field(key, state.policy.additional_credential_fields),
                )
                if finding is not None:
                    return finding
        finally:
            state.active_ids.remove(object_id)
        return None
    if type(value) in (list, tuple):
        if len(value) > state.policy.max_width:
            return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
        object_id = id(value)
        if object_id in state.active_ids:
            return CredentialMaterialFinding("unsupported_value", "traversal_boundary")
        state.active_ids.add(object_id)
        try:
            for child in value:
                finding = _find(
                    child,
                    state=state,
                    depth=depth + 1,
                    location="sequence_item",
                    credential_parent=False,
                )
                if finding is not None:
                    return finding
        finally:
            state.active_ids.remove(object_id)
        return None
    return CredentialMaterialFinding("unsupported_value", location)


def _is_empty(value: Any) -> bool:
    if value is None or (type(value) is str and value == ""):
        return True
    return type(value) in (dict, MappingProxyType, list, tuple) and len(value) == 0


def _allowed_reference_finding(
    value: Any,
    policy: CredentialTraversalPolicy,
) -> CredentialMaterialFinding | None | Literal[False]:
    if type(value) in (dict, MappingProxyType) and set(value) in ({"secret_ref"}, {"secret_ref", "secret_scope"}):
        ref = value["secret_ref"]
        scope = value["secret_scope"] if "secret_scope" in value else None
        if type(ref) is not str or not ref or (scope is not None and scope not in ("user", "server", "org")):
            return False
        ref_size = len(ref.encode("utf-8", errors="replace"))
        if ref_size > min(policy.max_string_bytes, _SECRET_REFERENCE_NAME_MAX_BYTES):
            return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
        category = _classify_string(ref)
        if category is not None:
            return CredentialMaterialFinding(category, "mapping_value")
        if _SECRET_REFERENCE_NAME_PATTERN.fullmatch(ref) is None:
            return False
        return None
    if type(value) is not str:
        return False
    match = _EXACT_ENV_VAR_REF_PATTERN.fullmatch(value)
    if match is None or match.group(1) not in policy.env_ref_names:
        return False
    if len(value.encode("utf-8", errors="replace")) > policy.max_string_bytes:
        return CredentialMaterialFinding("traversal_limit", "traversal_boundary")
    return None


def _classify_string(value: str) -> CredentialCategory | None:
    if _PRIVATE_KEY_PATTERN.search(value):
        return "private_key"
    if any(pattern.search(value) for pattern in _CREDENTIAL_URL_PATTERNS):
        return "credential_url"
    for candidate in _http_url_candidates(value):
        category = _classify_http_url(candidate)
        if category is not None:
            return category
    if any(pattern.search(value) for _name, pattern in CREDENTIAL_PATTERN_SPECS):
        return "token_shape"
    return None


def _http_url_candidates(value: str) -> tuple[str, ...]:
    parsed = _parse_url(value)
    standalone: tuple[str, ...]
    if parsed == _URL_PARSE_ERROR:
        standalone = (value,)
    else:
        assert type(parsed) is ParseResult
        standalone = (value,) if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ()
    embedded = tuple(match.group(0).rstrip(".,;:!?)]}") for match in _HTTP_URL_CANDIDATE_RE.finditer(value))
    return tuple(dict.fromkeys(standalone + embedded))


def _classify_http_url(value: str) -> CredentialCategory | None:
    parsed = _parse_url(value)
    if parsed == _URL_PARSE_ERROR:
        return "unsupported_value"
    assert type(parsed) is ParseResult
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return "credential_url"
    for encoded in (parsed.query, parsed.fragment):
        # parse_qs performs the boundary's single percent-decoding pass.
        params = parse_qs(encoded, keep_blank_values=True)
        for key, values in params.items():
            if _base_param_name(key.lower()) in SENSITIVE_PARAMS:
                return "sensitive_url_parameter"
            if any(_classify_decoded_url_value(item) is not None for item in values):
                return "sensitive_url_parameter"
    return None


def _parse_url(value: str) -> ParseResult | Literal["invalid-url"]:
    """Parse external URL text without allowing parser errors to escape."""
    try:
        return urlparse(value)
    except ValueError:
        return _URL_PARSE_ERROR


def _classify_decoded_url_value(value: str) -> CredentialCategory | None:
    """Classify one once-decoded query/fragment value without URL recursion."""
    if _PRIVATE_KEY_PATTERN.search(value):
        return "private_key"
    if any(pattern.search(value) for pattern in _CREDENTIAL_URL_PATTERNS):
        return "credential_url"
    if any(pattern.search(value) for _name, pattern in CREDENTIAL_PATTERN_SPECS):
        return "token_shape"
    return None


def _base_param_name(key: str) -> str:
    return key.split("[", 1)[0].split(".", 1)[0]


def scrub_credential_material(value: Any, *, parent_key: str | None = None) -> Any:
    """Return a deep credential-redacted copy using the detector authority.

    Audit scrubbing is a non-raising backstop.  Unsupported objects pass
    through unchanged; every recognized credential-bearing value becomes one
    fixed sentinel, avoiding partial disclosure through preserved structure.
    """
    return _scrub(value, parent_key=parent_key, active_ids=set(), depth=0, nodes=[0])


def _scrub(
    value: Any,
    *,
    parent_key: str | None,
    active_ids: set[int],
    depth: int,
    nodes: list[int],
) -> Any:
    nodes[0] += 1
    if depth > 32 or nodes[0] > 4096:
        return REDACTED_CREDENTIAL_TEXT
    if parent_key is not None and is_secret_field(parent_key):
        return REDACTED_CREDENTIAL_TEXT
    if type(value) in (dict, MappingProxyType):
        object_id = id(value)
        if object_id in active_ids or len(value) > 512:
            return REDACTED_CREDENTIAL_TEXT
        active_ids.add(object_id)
        try:
            result: dict[Any, Any] = {}
            for key, child in value.items():
                scrubbed_key = REDACTED_CREDENTIAL_TEXT if type(key) is str and _classify_string(key) is not None else key
                result[scrubbed_key] = _scrub(
                    child,
                    parent_key=key if type(key) is str else None,
                    active_ids=active_ids,
                    depth=depth + 1,
                    nodes=nodes,
                )
            return result
        finally:
            active_ids.remove(object_id)
    if type(value) is str:
        return REDACTED_CREDENTIAL_TEXT if _classify_string(value) is not None else value
    if type(value) in (list, tuple):
        object_id = id(value)
        if object_id in active_ids or len(value) > 512:
            return REDACTED_CREDENTIAL_TEXT
        active_ids.add(object_id)
        try:
            return [_scrub(child, parent_key=None, active_ids=active_ids, depth=depth + 1, nodes=nodes) for child in value]
        finally:
            active_ids.remove(object_id)
    if type(value) in (set, frozenset):
        return sorted(
            (_scrub(child, parent_key=None, active_ids=active_ids, depth=depth + 1, nodes=nodes) for child in value),
            key=repr,
        )
    return value
