"""Pure Tier-3 parsing helpers shared by the Azure asynchronous-analyze transforms.

No I/O. These functions interpret the parts of an Azure AI Document
Intelligence or Content Understanding response that the two services share:
the poll body's status envelope, the ``Operation-Location`` header, the
``{"error": {...}}`` envelope, list facets, and typed field values
(``{"type": "string", "valueString": ...}``). Each is a declared Tier-3
boundary: the raising parsers fail closed with ``MalformedResponseError`` so
the caller fails the row, the observing ones return None for anything that is
not the expected shape; nothing here coerces or fabricates a value.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from elspeth.contracts.trust_boundary import observation_boundary, trust_boundary
from elspeth.plugins.transforms.azure.errors import MalformedResponseError

# Azure error codes and warning codes are bounded identifier tokens. Anything
# else (prose, markup, a compromised response's payload) is not carried out of
# the boundary: the token is either admitted whole or dropped.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}")
# A field ``type`` names its value key (``string`` -> ``valueString``).
_FIELD_TYPE_PATTERN = re.compile(r"[a-z][A-Za-z]{0,63}")
# Nesting bound for ``valueArray`` / ``valueObject`` recursion.
_MAX_FIELD_DEPTH = 32


class AnalyzeStatus(enum.Enum):
    """The service-neutral reading of one poll body's ``status``."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """One parsed poll body: the status and, for a failure, its bounded cause."""

    status: AnalyzeStatus
    cause: str | None = None


class MalformedPollBodyError(MalformedResponseError):
    """A poll body the service contract does not allow; ``error_type`` names the defect."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


@observation_boundary(
    tier=3,
    source="a code, id or path segment read from an Azure analyze response or Operation-Location URL",
    source_param="value",
    suppresses=("R5",),
    invariant="returns value only when it is a str matching the bounded identifier-token grammar; every other value yields None",
)
def bounded_token(value: object) -> str | None:
    """Return ``value`` when it is a bounded identifier token, else None."""
    if isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None:
        return value
    return None


@observation_boundary(
    tier=3,
    source="a model or analyzer id the Azure analyze result reports about itself",
    source_param="value",
    suppresses=("R5",),
    invariant="returns value only when it is a str the configured id grammar fully matches; every other value yields None",
)
def reported_id(value: object, grammar: re.Pattern[str]) -> str | None:
    """The service-reported model/analyzer id, or None when it is not a valid id."""
    if isinstance(value, str) and grammar.fullmatch(value) is not None:
        return value
    return None


@observation_boundary(
    tier=3,
    source="Azure AI error response body (parsed JSON) of a rejected submit or poll call",
    source_param="body",
    suppresses=("R1", "R5"),
    invariant="returns (error.code, error.innererror.code) as bounded tokens; any absent or malformed member yields None and error.message is never read",
)
def azure_error_codes(body: object) -> tuple[str | None, str | None]:
    """Read ``(error.code, error.innererror.code)`` from an Azure error envelope.

    Never raises: the caller is already on a failure path and only wants the
    bounded codes as diagnostics.
    """
    if not isinstance(body, Mapping):
        return None, None
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None, None
    code = bounded_token(error.get("code"))
    inner = error.get("innererror")
    inner_code = bounded_token(inner.get("code")) if isinstance(inner, Mapping) else None
    return code, inner_code


@observation_boundary(
    tier=3,
    source="Azure analyze poll body (parsed JSON) of a failed or canceled operation",
    source_param="body",
    suppresses=("R1", "R5"),
    invariant="returns error.code truncated to 128 characters when that is a bounded token; any absent or malformed member yields None",
)
def failure_cause(body: Mapping[str, Any]) -> str | None:
    """The bounded failure cause of a failed operation's poll body.

    A code longer than the token bound is truncated to 128 characters only when
    the truncation is itself a token, so an oversized but well-formed code
    still identifies the failure.
    """
    error = body.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    if not isinstance(code, str):
        return None
    return bounded_token(code[:128])


@trust_boundary(
    tier=3,
    source="Azure analyze operation poll body (parsed JSON)",
    source_param="body",
    suppresses=("R1", "R5"),
    invariant=(
        "raises MalformedPollBodyError unless body is an object whose status is one of the service's operation "
        "statuses and, when that status is succeeded, whose result member is an object; never defaults a status"
    ),
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_parse_poll_body_rejects_malformed_bodies",
    test_fingerprint="a25cdfa60c537be724deaadbe9d78c3ea10fadf522b15c7ad61cf5a973b99a25",
)
def parse_poll_body(
    body: object, *, statuses: Mapping[str, AnalyzeStatus], result_key: str, missing_result_error: str
) -> tuple[PollOutcome, Mapping[str, Any] | None]:
    """Map one poll body onto ``(outcome, result)``; ``result`` is set only on success.

    A failed or canceled operation's cause is its bounded ``error.code`` or,
    when Azure sent none, the lower-cased status itself.
    """
    if not isinstance(body, Mapping):
        raise MalformedPollBodyError("unknown_status")
    status = body.get("status")
    if not isinstance(status, str) or status not in statuses:
        raise MalformedPollBodyError("unknown_status")
    mapped = statuses[status]
    if mapped is AnalyzeStatus.SUCCEEDED:
        result = body.get(result_key)
        if not isinstance(result, Mapping):
            raise MalformedPollBodyError(missing_result_error)
        return PollOutcome(AnalyzeStatus.SUCCEEDED), result
    if mapped is AnalyzeStatus.RUNNING:
        return PollOutcome(AnalyzeStatus.RUNNING), None
    cause = failure_cause(body)
    return PollOutcome(mapped, cause=cause if cause is not None else status.lower()), None


@trust_boundary(
    tier=3,
    source="Azure analyze result (parsed JSON) carrying an optional warnings list",
    source_param="result",
    suppresses=("R1", "R5"),
    invariant="raises MalformedResponseError when warnings is present but not a list of objects; a warning code that is not a bounded token is reported as unknown",
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_warning_codes_rejects_malformed_warnings",
    test_fingerprint="6f6b40a8eed7c8c4e71f28d4bc726e9d072a4f75edd47f73b0f32deab4f4fb68",
)
def warning_codes(result: Mapping[str, Any]) -> list[str]:
    """Bounded ``code`` of every entry in ``result.warnings`` (absent -> [])."""
    warnings = facet_list(result, "warnings")
    codes: list[str] = []
    for warning in warnings:
        if not isinstance(warning, Mapping):
            raise MalformedResponseError(f"warnings entries must be objects, got {type(warning).__name__}")
        code = bounded_token(warning.get("code"))
        codes.append(code if code is not None else "unknown")
    return codes


@trust_boundary(
    tier=3,
    source="an object (analyze result or result content) from an Azure analyze response",
    source_param="container",
    suppresses=("R1", "R5"),
    invariant="raises MalformedResponseError when the named member is present but not a list; an absent member is an empty list, never fabricated",
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_facet_list_rejects_a_non_list_member",
    test_fingerprint="af7b4e20c0c7cd7c5c887617cdd952933baa3e6a36dfa4d2ce562d678daae9b2",
)
def facet_list(container: Mapping[str, Any], key: str) -> list[Any]:
    """Return ``container[key]`` (a list), or [] when absent. Fail closed on wrong type."""
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedResponseError(f"{key} must be a list, got {type(value).__name__}")
    return value


@trust_boundary(
    tier=3,
    source="the fields map of an Azure analyze document or content (parsed JSON)",
    source_param="fields",
    suppresses=("R1", "R5"),
    invariant=(
        "raises MalformedResponseError unless fields is absent or an object of str-named fields, each an object with a "
        "lowerCamelCase type whose array/object values are well-formed and nest at most 32 levels; never coerces a value"
    ),
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_flatten_fields_malformed_fails_closed",
    test_fingerprint="7938dfff30c03de70d16f9e0cc06f3037a4c3b2f88df15420a673393804e6acd",
)
def flatten_fields(fields: object) -> dict[str, Any]:
    """Project a ``{name: typed field}`` map onto ``{name: plain value}``.

    Azure reports each extracted field as ``{"type": T, "value<T>": v, ...}``
    with ``confidence``/``spans`` alongside. The projection keeps only the
    value: arrays and objects recurse, any other type passes its ``value<T>``
    through as Azure sent it (a mapping for DI currency/address values), and a
    field Azure detected but could not normalise has no value key and projects
    to None. ``fields`` absent (None) is an empty map.
    """
    if fields is None:
        return {}
    return _flatten_field_map(fields, 0)


@trust_boundary(
    tier=3,
    source="a nested fields map inside an Azure analyze field value (parsed JSON)",
    source_param="fields",
    suppresses=("R1", "R5"),
    invariant="raises MalformedResponseError unless fields is an object whose every key is a str and every value a well-formed typed field",
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_flatten_field_map_rejects_malformed_maps",
    test_fingerprint="3c62ff6f5d5fa0da78753572aa9ab36a50c52572c1b5c07e39e1ae8265aa738e",
)
def _flatten_field_map(fields: object, depth: int) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        raise MalformedResponseError(f"fields must be an object, got {type(fields).__name__}")
    flattened: dict[str, Any] = {}
    for name, field in fields.items():
        if not isinstance(name, str):
            raise MalformedResponseError("field names must be strings")
        flattened[name] = _flatten_field(field, depth)
    return flattened


@trust_boundary(
    tier=3,
    source="one typed field value inside an Azure analyze fields map (parsed JSON)",
    source_param="field",
    suppresses=("R1", "R5"),
    invariant=(
        "raises MalformedResponseError unless field is an object with a lowerCamelCase str type, a list valueArray "
        "and an object valueObject when present, nested at most 32 levels; other value<T> members pass through unchanged"
    ),
    test_ref="tests/unit/plugins/transforms/azure/test_analyze_result.py::test_flatten_field_rejects_malformed_fields",
    test_fingerprint="a1cbca03645bbf4eed6ad74fcdbbfef93f1ef87f567a226cba090a5afa282839",
)
def _flatten_field(field: object, depth: int) -> Any:
    if depth > _MAX_FIELD_DEPTH:
        raise MalformedResponseError(f"field values nest deeper than {_MAX_FIELD_DEPTH} levels")
    if not isinstance(field, Mapping):
        raise MalformedResponseError(f"a field must be an object, got {type(field).__name__}")
    field_type = field.get("type")
    if not isinstance(field_type, str) or _FIELD_TYPE_PATTERN.fullmatch(field_type) is None:
        raise MalformedResponseError("a field must carry a lowerCamelCase string type")
    value_key = f"value{field_type[0].upper()}{field_type[1:]}"
    if field_type == "array":
        items = field.get(value_key)
        if items is None:
            return []
        if not isinstance(items, list):
            raise MalformedResponseError(f"valueArray must be a list, got {type(items).__name__}")
        return [_flatten_field(item, depth + 1) for item in items]
    if field_type == "object":
        members = field.get(value_key)
        if members is None:
            return {}
        return _flatten_field_map(members, depth + 1)
    return field.get(value_key)


def operation_location_host_matches(operation_url: str, endpoint: str) -> bool:
    """True iff operation_url is a well-formed HTTPS URL on the same host AND port as endpoint.

    Guards against the polled Operation-Location (which carries our api-key header)
    being pointed elsewhere by a malformed/compromised 202 response. We refuse to
    follow it unless its scheme is https and its host AND port both match the
    operator-configured endpoint, so the api-key is never sent to a different
    origin (host:port) than the operator configured.
    """
    try:
        op = urlparse(operation_url)
        ep = urlparse(endpoint)
        # ParseResult.port performs validation lazily and can raise for malformed
        # or out-of-range ports, so keep access inside the fail-closed guard.
        parsed_op_port = op.port
        parsed_ep_port = ep.port
        op_port = parsed_op_port if parsed_op_port is not None else 443
        ep_port = parsed_ep_port if parsed_ep_port is not None else 443
    except ValueError:
        return False
    if op.scheme != "https" or not op.hostname:
        return False
    if op.hostname.lower() != (ep.hostname or "").lower():
        return False
    # Compare effective ports, normalizing the https default (443) so an explicit
    # ":443" matches an absent port. A same-host but attacker-port URL is rejected.
    return op_port == ep_port


def operation_id_from_url(operation_url: str, *, collection: str) -> str | None:
    """Extract the result id from ``.../{collection}/{id}[?...]``, or None when absent."""
    path = urlparse(operation_url).path
    segments = [segment for segment in path.split("/") if segment]
    for index, segment in enumerate(segments):
        if segment == collection and index + 1 < len(segments):
            return bounded_token(segments[index + 1])
    return None
