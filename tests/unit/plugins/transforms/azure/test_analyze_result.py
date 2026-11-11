"""Unit tests for the Tier-3 helpers shared by the Azure asynchronous-analyze transforms."""

from __future__ import annotations

import re
from typing import Any

import pytest

from elspeth.plugins.transforms.azure.analyze_result import (
    AnalyzeStatus,
    MalformedPollBodyError,
    PollOutcome,
    _flatten_field,
    _flatten_field_map,
    azure_error_codes,
    bounded_token,
    facet_list,
    failure_cause,
    flatten_fields,
    operation_id_from_url,
    operation_location_host_matches,
    parse_poll_body,
    reported_id,
    warning_codes,
)
from elspeth.plugins.transforms.azure.errors import MalformedResponseError

_ENDPOINT = "https://di.cognitiveservices.azure.com"
_OP = (
    "https://di.cognitiveservices.azure.com/documentintelligence/documentModels/"
    "prebuilt-layout/analyzeResults/3b31320d-8bab?api-version=2024-11-30"
)
_CU_OP = "https://cu.services.ai.azure.com/contentunderstanding/analyzerResults/7d1c-42?api-version=2025-11-01"


# ── Operation-Location host pinning ───────────────────────────────────────


def test_host_match_accepts_same_host_https() -> None:
    assert operation_location_host_matches(_OP, _ENDPOINT)


def test_host_match_rejects_other_host() -> None:
    assert not operation_location_host_matches("https://evil.example.com/analyzeResults/abc", _ENDPOINT)


def test_host_match_rejects_http_scheme() -> None:
    assert not operation_location_host_matches("http://di.cognitiveservices.azure.com/x", _ENDPOINT)


def test_host_match_rejects_same_host_different_port() -> None:
    # api-key must not be sent to a same-host attacker port (F1).
    assert not operation_location_host_matches("https://di.cognitiveservices.azure.com:8443/analyzeResults/abc", _ENDPOINT)


def test_host_match_accepts_explicit_default_port() -> None:
    # An explicit :443 matches an endpoint with no port (both normalize to 443).
    assert operation_location_host_matches("https://di.cognitiveservices.azure.com:443/analyzeResults/abc", _ENDPOINT)


@pytest.mark.parametrize("port", ["invalid", "65536", "-1"])
def test_host_match_rejects_malformed_operation_port(port: str) -> None:
    assert not operation_location_host_matches(f"https://di.cognitiveservices.azure.com:{port}/analyzeResults/abc", _ENDPOINT)


def test_host_match_rejects_malformed_endpoint_port() -> None:
    assert not operation_location_host_matches(_OP, "https://di.cognitiveservices.azure.com:invalid")


def test_host_match_rejects_garbage() -> None:
    assert not operation_location_host_matches("not a url", _ENDPOINT)


def test_host_match_is_case_insensitive() -> None:
    assert operation_location_host_matches("https://DI.CognitiveServices.Azure.Com/analyzeResults/abc", _ENDPOINT)


# ── Operation id ──────────────────────────────────────────────────────────


def test_operation_id_from_url_per_service_collection() -> None:
    assert operation_id_from_url(_OP, collection="analyzeResults") == "3b31320d-8bab"
    assert operation_id_from_url(_CU_OP, collection="analyzerResults") == "7d1c-42"
    assert operation_id_from_url(_CU_OP, collection="analyzeResults") is None
    assert operation_id_from_url("https://h/no/result/segment", collection="analyzeResults") is None


def test_operation_id_that_is_not_a_token_is_dropped() -> None:
    assert operation_id_from_url("https://h/analyzeResults/%3Cscript%3E", collection="analyzeResults") is None


# ── Bounded tokens, error envelopes, warnings ─────────────────────────────


@pytest.mark.parametrize("value", ["InvalidRequest", "Invalid.Content-Length_2", "X" * 128])
def test_bounded_token_admits_identifier_tokens(value: str) -> None:
    assert bounded_token(value) == value


@pytest.mark.parametrize("value", ["", "X" * 129, "has space", "<b>", "-leading", 7, None, ["InvalidRequest"]])
def test_bounded_token_drops_everything_else(value: object) -> None:
    assert bounded_token(value) is None


def test_azure_error_codes_reads_code_and_inner_code_never_message() -> None:
    body = {"error": {"code": "InvalidRequest", "message": "secret detail", "innererror": {"code": "InvalidContentLength"}}}
    assert azure_error_codes(body) == ("InvalidRequest", "InvalidContentLength")


@pytest.mark.parametrize(
    "body",
    [None, "text", [], {}, {"error": "x"}, {"error": {"code": 5}}, {"error": {"code": "has space", "innererror": "x"}}],
)
def test_azure_error_codes_never_raises_on_malformed_envelopes(body: Any) -> None:
    assert azure_error_codes(body) == (None, None)


def test_failure_cause_truncates_an_oversized_token() -> None:
    assert failure_cause({"error": {"code": "X" * 500}}) == "X" * 128
    assert failure_cause({"error": {"code": "Bad code!"}}) is None
    assert failure_cause({"status": "failed"}) is None


def test_warning_codes() -> None:
    assert warning_codes({}) == []
    assert warning_codes({"warnings": [{"code": "W1"}, {"code": "has space"}, {}]}) == ["W1", "unknown", "unknown"]


@pytest.mark.parametrize("result", [{"warnings": ["not-an-object"]}, {"warnings": {"code": "W1"}}, {"warnings": "W1"}])
def test_warning_codes_rejects_malformed_warnings(result: dict[str, Any]) -> None:
    # ``@trust_boundary`` honesty proof for ``warning_codes``'s ``result`` parameter.
    with pytest.raises(MalformedResponseError):
        warning_codes(result)


def test_facet_list() -> None:
    assert facet_list({"tables": [1]}, "tables") == [1]
    assert facet_list({}, "tables") == []


@pytest.mark.parametrize("container", [{"tables": "x"}, {"tables": {"rowCount": 1}}, {"tables": 3}])
def test_facet_list_rejects_a_non_list_member(container: dict[str, Any]) -> None:
    # ``@trust_boundary`` honesty proof for ``facet_list``'s ``container`` parameter.
    with pytest.raises(MalformedResponseError):
        facet_list(container, "tables")


# ── Poll body ─────────────────────────────────────────────────────────────

_STATUSES = {
    "running": AnalyzeStatus.RUNNING,
    "succeeded": AnalyzeStatus.SUCCEEDED,
    "failed": AnalyzeStatus.FAILED,
    "Canceled": AnalyzeStatus.CANCELED,
}


def _parse(body: object) -> tuple[PollOutcome, object]:
    return parse_poll_body(body, statuses=_STATUSES, result_key="result", missing_result_error="missing_result")


def test_parse_poll_body_maps_each_status() -> None:
    assert _parse({"status": "running"}) == (PollOutcome(AnalyzeStatus.RUNNING), None)
    assert _parse({"status": "succeeded", "result": {"a": 1}}) == (PollOutcome(AnalyzeStatus.SUCCEEDED), {"a": 1})
    assert _parse({"status": "failed", "error": {"code": "Bad", "message": "secret"}}) == (
        PollOutcome(AnalyzeStatus.FAILED, cause="Bad"),
        None,
    )
    assert _parse({"status": "failed"}) == (PollOutcome(AnalyzeStatus.FAILED, cause="failed"), None)
    assert _parse({"status": "Canceled"}) == (PollOutcome(AnalyzeStatus.CANCELED, cause="canceled"), None)


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        (["not", "an", "object"], "unknown_status"),
        ({}, "unknown_status"),
        ({"status": 1}, "unknown_status"),
        ({"status": "Running"}, "unknown_status"),  # the vocabulary is case-exact per service
        ({"status": "succeeded"}, "missing_result"),
        ({"status": "succeeded", "result": ["not", "an", "object"]}, "missing_result"),
    ],
)
def test_parse_poll_body_rejects_malformed_bodies(body: object, error_type: str) -> None:
    # ``@trust_boundary`` honesty proof for ``parse_poll_body``'s ``body`` parameter.
    with pytest.raises(MalformedPollBodyError) as excinfo:
        parse_poll_body(body, statuses=_STATUSES, result_key="result", missing_result_error="missing_result")
    assert excinfo.value.error_type == error_type


# ── Reported ids ──────────────────────────────────────────────────────────


def test_reported_id_admits_only_grammar_matches() -> None:
    grammar = re.compile(r"[a-z-]{1,20}")
    assert reported_id("prebuilt-layout", grammar) == "prebuilt-layout"
    assert reported_id("<script>", grammar) is None
    assert reported_id(7, grammar) is None
    assert reported_id(None, grammar) is None


# ── Typed field flattening ────────────────────────────────────────────────


def test_flatten_fields_projects_scalars_arrays_and_objects() -> None:
    fields = {
        "Name": {"type": "string", "valueString": "Contoso", "confidence": 0.98, "spans": [{"offset": 0, "length": 7}]},
        "Total": {"type": "number", "valueNumber": 110.5},
        "Count": {"type": "integer", "valueInteger": 3},
        "Paid": {"type": "boolean", "valueBoolean": False},
        "Due": {"type": "date", "valueDate": "2026-11-30"},
        "Raw": {"type": "json", "valueJson": {"free": ["form"]}},
        "Items": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {"Sku": {"type": "string", "valueString": "A1"}, "Qty": {"type": "integer", "valueInteger": 2}},
                }
            ],
        },
        "Undetected": {"type": "string", "confidence": 0.1},
        "EmptyList": {"type": "array"},
        "EmptyObject": {"type": "object"},
    }
    assert flatten_fields(fields) == {
        "Name": "Contoso",
        "Total": 110.5,
        "Count": 3,
        "Paid": False,
        "Due": "2026-11-30",
        "Raw": {"free": ["form"]},
        "Items": [{"Sku": "A1", "Qty": 2}],
        "Undetected": None,
        "EmptyList": [],
        "EmptyObject": {},
    }


def test_flatten_fields_absent_is_empty() -> None:
    assert flatten_fields(None) == {}


@pytest.mark.parametrize(
    "fields",
    [
        ["not", "a", "map"],
        {"A": "not-a-field"},
        {"A": {"valueString": "no type"}},
        {"A": {"type": "String", "valueString": "bad casing"}},
        {"A": {"type": "value String"}},
        {"A": {"type": "array", "valueArray": {"not": "a list"}}},
        {"A": {"type": "object", "valueObject": ["not", "a", "map"]}},
    ],
)
def test_flatten_fields_malformed_fails_closed(fields: object) -> None:
    with pytest.raises(MalformedResponseError):
        flatten_fields(fields)


def test_flatten_fields_bounds_nesting_depth() -> None:
    field: dict[str, Any] = {"type": "string", "valueString": "leaf"}
    for _ in range(40):
        field = {"type": "array", "valueArray": [field]}
    with pytest.raises(MalformedResponseError, match="nest deeper"):
        flatten_fields({"Deep": field})


@pytest.mark.parametrize("fields", ["not-a-map", ["a"], {1: {"type": "string"}}, {"A": "not-a-field"}])
def test_flatten_field_map_rejects_malformed_maps(fields: object) -> None:
    # ``@trust_boundary`` honesty proof for ``_flatten_field_map``'s ``fields`` parameter.
    with pytest.raises(MalformedResponseError):
        _flatten_field_map(fields, 0)


@pytest.mark.parametrize(
    "field",
    [
        "not-an-object",
        {"valueString": "no type"},
        {"type": "String"},
        {"type": "array", "valueArray": "x"},
        {"type": "object", "valueObject": ["x"]},
    ],
)
def test_flatten_field_rejects_malformed_fields(field: object) -> None:
    # ``@trust_boundary`` honesty proof for ``_flatten_field``'s ``field`` parameter.
    with pytest.raises(MalformedResponseError):
        _flatten_field(field, 0)
