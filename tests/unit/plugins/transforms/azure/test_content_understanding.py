"""Unit tests for the azure_content_understanding transform."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx

import elspeth.plugins.transforms.azure.analyze_lro as analyze_lro_module
from elspeth.contracts import CallType, Determinism
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine.executors.declared_output_types import verify_produced_output_types
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.azure.content_understanding import (
    AzureContentUnderstanding,
    AzureContentUnderstandingConfig,
)
from elspeth.plugins.transforms.azure.content_understanding_result import parse_result_contents
from elspeth.plugins.transforms.azure.errors import MalformedResponseError
from tests.fixtures.factories import make_context

_ENDPOINT = "https://test.services.ai.azure.com"
OP = f"{_ENDPOINT}/contentunderstanding/analyzerResults/op-123?api-version=2025-11-01"

BASE: dict[str, Any] = {
    "endpoint": _ENDPOINT,
    "api_key": "k",
    "analyzer_id": "prebuilt-invoice",
    "source_mode": "url",
    "source_field": "doc_url",
    "content_field": "cu_markdown",
    "schema": {"mode": "observed"},
}

_DOC = {
    "kind": "document",
    "mimeType": "application/pdf",
    "markdown": "# Invoice\n\nTotal 110",
    "startPageNumber": 1,
    "endPageNumber": 2,
    "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
    "tables": [{"rowCount": 2, "columnCount": 2, "cells": []}],
    "fields": {
        "VendorName": {"type": "string", "valueString": "Contoso", "confidence": 0.97},
        "InvoiceTotal": {"type": "number", "valueNumber": 110.0},
        "LineItems": {
            "type": "array",
            "valueArray": [{"type": "object", "valueObject": {"Sku": {"type": "string", "valueString": "A1"}}}],
        },
    },
}


def _succeeded(*contents: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "op-123", "status": "Succeeded", "result": {"analyzerId": "prebuilt-invoice", "contents": list(contents), **extra}}


# ── Config ─────────────────────────────────────────────────────────────────


def _cfg(**overrides: Any) -> AzureContentUnderstandingConfig:
    return AzureContentUnderstandingConfig.from_dict({**BASE, **overrides}, plugin_name="azure_content_understanding")


def test_valid_minimal_config() -> None:
    cfg = _cfg()
    assert cfg.api_version == "2025-11-01"
    assert cfg.all_output_field_names() == ["cu_markdown"]
    assert cfg.configured_output_fields() == {}
    assert cfg.max_capacity_retry_seconds == 300


@pytest.mark.parametrize("endpoint", ["https://res.services.ai.azure.com", "https://res.cognitiveservices.azure.com/"])
def test_accepts_foundry_and_ai_services_hosts(endpoint: str) -> None:
    assert _cfg(endpoint=endpoint).endpoint == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://res.services.ai.azure.com",
        "https://attacker.example",
        "https://services.ai.azure.com",
        "https://res.services.ai.azure.com.attacker.example",
        "https://res.services.ai.azure.com:8443",
        "https://res.services.ai.azure.com/contentunderstanding",
        "https://res.services.ai.azure.com?x=1",
    ],
)
def test_rejects_non_origin_or_non_azure_endpoint(endpoint: str) -> None:
    with pytest.raises(PluginConfigError):
        _cfg(endpoint=endpoint)


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "  "},
        {"api_version": "2025-05-01-preview"},
        {"analyzer_id": "bad/analyzer"},
        {"analyzer_id": "x" * 65},
        {"source_mode": "ftp"},
        {"source_field": " "},
        {"content_range": "1;2"},
        {"mime_type": "PDF"},
        {"processing_location": "moon"},
        {"string_encoding": "latin1"},
        {"model_deployments": {"gpt-4.1": "bad deployment"}},
        {"model_deployments": {f"m{i}": "d" for i in range(17)}},
        {"poll_interval_seconds": 5.0, "poll_max_interval_seconds": 1.0},
        {"content_field": "dup", "fields_field": "dup"},
        {"extract": {"bogus": "x"}},
    ],
)
def test_rejects_invalid_options(overrides: dict[str, Any]) -> None:
    with pytest.raises(PluginConfigError):
        _cfg(**overrides)


def test_requires_at_least_one_output() -> None:
    data = {key: value for key, value in BASE.items() if key != "content_field"}
    with pytest.raises(PluginConfigError, match="At least one output target"):
        AzureContentUnderstandingConfig.from_dict(data, plugin_name="azure_content_understanding")


@pytest.mark.parametrize("value", ["1-3,5,9-", "7", "0-60000"])
def test_accepts_page_and_millisecond_ranges(value: str) -> None:
    assert _cfg(content_range=value).content_range == value


def test_extract_maps_to_content_keys() -> None:
    cfg = _cfg(extract={"tables": "t", "transcript_phrases": "tp", "contents": "c"})
    assert cfg.configured_output_fields() == {"contents": "c", "tables": "t", "transcriptPhrases": "tp"}
    assert cfg.all_output_field_names() == ["cu_markdown", "c", "t", "tp"]


# ── Transform metadata ─────────────────────────────────────────────────────


def _transform(**overrides: Any) -> AzureContentUnderstanding:
    return AzureContentUnderstanding({**BASE, **overrides})


def test_transform_metadata_and_declared_fields() -> None:
    t = _transform(fields_field="cu_fields", page_count_field="cu_pages", extract={"tables": "cu_tables"})
    assert t.name == "azure_content_understanding"
    assert t.determinism is Determinism.EXTERNAL_CALL
    assert t.passes_through_input is True
    assert t.declared_output_fields == frozenset({"cu_markdown", "cu_fields", "cu_pages", "cu_tables"})
    assert t.declared_input_fields == frozenset({"doc_url"})
    assert t.declared_string_input_fields == frozenset({"doc_url"})
    assert t.discovery_secret_requirements == {"api_key": ("AZURE_CONTENT_UNDERSTANDING_KEY",)}


def test_probe_config_instantiates() -> None:
    AzureContentUnderstanding(AzureContentUnderstanding.probe_config())


def test_source_field_naming_a_created_field_is_rejected() -> None:
    with pytest.raises(PluginConfigError, match="source_field names 'cu_markdown', which azure_content_understanding itself creates"):
        _transform(source_field="cu_markdown")


def test_output_schema_admits_created_fields_under_fixed_schema() -> None:
    t = _transform(schema={"mode": "fixed", "fields": ["doc_url: str"]})
    validated = t.output_schema.model_validate({"doc_url": "https://x/y.pdf", "cu_markdown": "text"}, strict=True)
    assert validated.cu_markdown == "text"


def test_process_raises_use_accept() -> None:
    with pytest.raises(NotImplementedError):
        _transform().process(Mock(spec_set=PipelineRow), Mock(spec_set=["state_id", "token"]))


def test_default_request_headers_carry_api_key() -> None:
    assert _transform()._default_request_headers() == {"Ocp-Apim-Subscription-Key": "k"}


def test_analyze_url_and_query_params() -> None:
    assert _transform()._analyze_url() == f"{_ENDPOINT}/contentunderstanding/analyzers/prebuilt-invoice:analyze?api-version=2025-11-01"
    url = _transform(string_encoding="utf16", processing_location="dataZone")._analyze_url()
    assert url.endswith(":analyze?api-version=2025-11-01&stringEncoding=utf16&processingLocation=dataZone")


# ── Fake-client harness ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, *, headers: dict[str, str] | None = None, body: Any = None) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.text = json.dumps(body if body is not None else {})
        self.content = self.text.encode()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://x")
            response = httpx.Response(self.status_code, request=request, headers=self.headers, text=self.text)
            raise httpx.HTTPStatusError("err", request=request, response=response)


class _FakeClient:
    def __init__(self, post_resps: list[Any] | Any, get_resps: list[Any]) -> None:
        self._post_resps = post_resps if isinstance(post_resps, list) else [post_resps]
        self._get_resps = list(get_resps)
        self.posts: list[tuple[Any, Any, str]] = []
        self.gets: list[str] = []

    def post(self, url: str, *, json: Any = None, audit_json: Any = None, timeout: float | None = None) -> Any:
        del timeout
        self.posts.append((json, audit_json, url))
        return self._post_resps.pop(0)

    def get(self, url: str, *, timeout: float | None = None) -> Any:
        del timeout
        self.gets.append(url)
        return self._get_resps.pop(0)

    def close(self) -> None:
        return None


def _row(value: str = "https://x/y.pdf", field: str = "doc_url") -> PipelineRow:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            FieldContract(normalized_name=field, original_name=field, python_type=str, required=False, source="inferred", nullable=False),
        ),
        locked=True,
    )
    return PipelineRow({field: value}, contract)


def _run(t: AzureContentUnderstanding, fake: _FakeClient, row: PipelineRow | None = None) -> Any:
    t._poll_interval_seconds = 0.0
    t._poll_max_interval_seconds = 0.0
    with t._http_clients_lock:
        t._http_clients["s1"] = fake
    try:
        return t._process_single_with_state(row if row is not None else _row(), "s1", ctx=make_context(), token_id=None)
    finally:
        with t._http_clients_lock:
            t._http_clients.pop("s1", None)


def _post_202() -> _Resp:
    return _Resp(202, headers={"operation-location": OP})


# ── Happy path and outputs ─────────────────────────────────────────────────


def test_happy_path_enriches_every_output() -> None:
    t = _transform(
        fields_field="cu_fields",
        page_count_field="cu_pages",
        metadata_field="cu_meta",
        result_field="cu_result",
        extract={"tables": "cu_tables", "pages": "cu_page_list", "contents": "cu_contents"},
    )
    body = _succeeded(_DOC, warnings=[{"code": "LowConfidence", "message": "ignore"}])
    fake = _FakeClient(_post_202(), [_Resp(200, body={"id": "op-123", "status": "Running"}), _Resp(200, body=body)])

    result = _run(t, fake)

    assert result.status == "success"
    out = result.row.to_dict()
    assert out["doc_url"] == "https://x/y.pdf"
    assert out["cu_markdown"] == "# Invoice\n\nTotal 110"
    assert out["cu_fields"] == {"VendorName": "Contoso", "InvoiceTotal": 110.0, "LineItems": [{"Sku": "A1"}]}
    assert out["cu_pages"] == 2
    assert out["cu_tables"] == _DOC["tables"]
    assert out["cu_page_list"] == _DOC["pages"]
    assert out["cu_contents"] == [_DOC]
    assert out["cu_result"] == body["result"]
    assert out["cu_meta"] == {
        "analyzer_id": "prebuilt-invoice",
        "api_version": "2025-11-01",
        "operation_id": "op-123",
        "content_count": 1,
        "page_count": 2,
        "warning_count": 1,
        "warning_codes": ["LowConfidence"],
        "result_status": "succeeded",
    }
    assert result.success_reason["action"] == "enriched"
    assert result.success_reason["metadata"] == out["cu_meta"]
    assert fake.gets == [OP, OP]
    declared = {name: contract.python_type for name, contract in t._stamped_output_field_contracts().items()}
    assert declared["cu_markdown"] is str
    assert declared["cu_pages"] is int
    assert declared["cu_fields"] is object
    verify_produced_output_types(transform=t, input_row=_row(), emitted_rows=[result.row])


def test_multiple_contents_join_markdown_and_concatenate_facets() -> None:
    t = _transform(page_count_field="p", fields_field="f", extract={"tables": "t", "transcript_phrases": "tp"})
    second = {
        "kind": "document",
        "mimeType": "application/pdf",
        "markdown": "Page 3",
        "startPageNumber": 3,
        "endPageNumber": 3,
        "tables": [{"rowCount": 9}],
    }
    audio = {
        "kind": "audioVisual",
        "mimeType": "audio/wav",
        "markdown": "Speech",
        "startTimeMs": 0,
        "endTimeMs": 900,
        "transcriptPhrases": [{"text": "hi"}],
    }
    result = _run(t, _FakeClient(_post_202(), [_Resp(200, body=_succeeded(_DOC, second, audio))]))
    out = result.row.to_dict()
    assert out["cu_markdown"] == "# Invoice\n\nTotal 110\n\nPage 3\n\nSpeech"
    assert out["p"] == 3  # audio contents carry no pages
    assert out["t"] == [*_DOC["tables"], {"rowCount": 9}]
    assert out["tp"] == [{"text": "hi"}]
    assert out["f"] == {"VendorName": "Contoso", "InvoiceTotal": 110.0, "LineItems": [{"Sku": "A1"}]}
    assert result.success_reason["metadata"]["content_count"] == 3


def test_absent_optional_parts_are_empty_not_fabricated() -> None:
    t = _transform(fields_field="f", extract={"tables": "t"})
    bare = {"kind": "document", "mimeType": "application/pdf", "startPageNumber": 1, "endPageNumber": 1}
    out = _run(t, _FakeClient(_post_202(), [_Resp(200, body=_succeeded(bare))])).row.to_dict()
    assert out == {"doc_url": "https://x/y.pdf", "cu_markdown": "", "f": {}, "t": []}


def test_empty_contents_list_is_a_successful_empty_analysis() -> None:
    t = _transform(fields_field="f", page_count_field="p")
    out = _run(t, _FakeClient(_post_202(), [_Resp(200, body=_succeeded())])).row.to_dict()
    assert (out["cu_markdown"], out["f"], out["p"]) == ("", {}, 0)


# ── Request bodies and the audit projection ────────────────────────────────

_SAS_URL = "https://acct.blob.core.windows.net/media/a.mp4?sv=2024-11-04&sp=r&sig=SECRETSIGVALUE%3D"


def test_url_request_body_and_fingerprinted_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "unit-test-fingerprint-key")
    monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)
    t = _transform(content_range="1-3", mime_type="application/pdf", model_deployments={"gpt-4.1": "my-gpt"})
    fake = _FakeClient(_post_202(), [_Resp(200, body=_succeeded(_DOC))])

    _run(t, fake, row=_row(_SAS_URL))

    [(wire, audit, url)] = fake.posts
    assert url == f"{_ENDPOINT}/contentunderstanding/analyzers/prebuilt-invoice:analyze?api-version=2025-11-01"
    assert wire == {
        "inputs": [{"url": _SAS_URL, "mimeType": "application/pdf", "range": "1-3"}],
        "modelDeployments": {"gpt-4.1": "my-gpt"},
    }
    assert audit["modelDeployments"] == {"gpt-4.1": "my-gpt"}
    [audited_input] = audit["inputs"]
    assert audited_input["range"] == "1-3"
    assert "SECRETSIGVALUE" not in audited_input["url"]
    assert "sig=%3Cfingerprint%3A" in audited_input["url"]


def test_base64_request_body() -> None:
    t = _transform(source_mode="base64", source_field="doc_b64", mime_type="image/png")
    fake = _FakeClient(_post_202(), [_Resp(200, body=_succeeded(_DOC))])
    result = _run(t, fake, row=_row("iVBORw0KGgo=", field="doc_b64"))
    assert result.status == "success"
    [(wire, audit, _url)] = fake.posts
    assert wire == audit == {"inputs": [{"data": "iVBORw0KGgo=", "mimeType": "image/png"}]}


@pytest.mark.parametrize("value", ["data:image/png;base64,iVBORw0KGgo=", "not base64!", "iVBORw0KGgo"])
def test_invalid_base64_never_reaches_azure(value: str) -> None:
    t = _transform(source_mode="base64", source_field="doc_b64")
    fake = _FakeClient([], [])
    result = _run(t, fake, row=_row(value, field="doc_b64"))
    assert result.reason == {"reason": "invalid_input", "field": "doc_b64", "error_type": "invalid_base64"}
    assert fake.posts == []


def test_base64_too_large() -> None:
    t = _transform(source_mode="base64", source_field="doc_b64", max_base64_chars=4)
    result = _run(t, _FakeClient([], []), row=_row("AAAAAAAA", field="doc_b64"))
    assert result.reason["error_type"] == "base64_too_large"


# ── Input errors ───────────────────────────────────────────────────────────


def test_missing_and_non_string_and_invalid_url_sources() -> None:
    t = _transform()
    empty = PipelineRow({}, _row().contract)
    assert _run(t, _FakeClient([], []), row=empty).reason == {"reason": "missing_field", "field": "doc_url"}
    assert _run(t, _FakeClient([], []), row=_row("  ")).reason["reason"] == "non_string_field"
    assert _run(t, _FakeClient([], []), row=_row("ftp://x/y")).reason["error_type"] == "invalid_document_url"


# ── Operation lifecycle failures ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "status", "cause"),
    [
        ({"id": "op", "status": "Failed", "error": {"code": "InvalidContent", "message": "leak"}}, "failed", "InvalidContent"),
        ({"id": "op", "status": "Failed"}, "failed", "failed"),
        ({"id": "op", "status": "Canceled"}, "canceled", "canceled"),
    ],
)
def test_failed_and_canceled_operations(body: dict[str, Any], status: str, cause: str) -> None:
    result = _run(_transform(), _FakeClient(_post_202(), [_Resp(200, body=body)]))
    assert result.reason == {"reason": "analysis_failed", "error_type": status, "cause": cause}
    assert "leak" not in json.dumps(result.reason)


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        ({"id": "op", "status": "succeeded"}, "unknown_status"),  # DI casing is not CU casing
        ({"id": "op", "status": "Weird"}, "unknown_status"),
        (["not", "an", "object"], "unknown_status"),
        ({"id": "op", "status": "Succeeded"}, "missing_result"),
        ({"id": "op", "status": "Succeeded", "result": "x"}, "missing_result"),
    ],
)
def test_malformed_poll_bodies(body: Any, error_type: str) -> None:
    result = _run(_transform(), _FakeClient(_post_202(), [_Resp(200, body=body)]))
    assert result.reason == {"reason": "malformed_response", "error_type": error_type}


@pytest.mark.parametrize(
    "result_body",
    [
        {"analyzerId": "a"},  # contents is required
        {"contents": "x"},
        {"contents": ["x"]},
        {"contents": [{"kind": "document", "markdown": 5, "startPageNumber": 1, "endPageNumber": 1}]},
        {"contents": [{"kind": "document", "markdown": "m", "startPageNumber": 3, "endPageNumber": 1}]},
        {"contents": [{"kind": "document", "markdown": "m", "startPageNumber": True, "endPageNumber": 1}]},
        {"contents": [{"kind": "document", "markdown": "m"}]},
        {"contents": [], "warnings": "x"},
    ],
)
def test_malformed_results_fail_the_row_closed(result_body: dict[str, Any]) -> None:
    result = _run(
        _transform(page_count_field="p"),
        _FakeClient(_post_202(), [_Resp(200, body={"id": "op", "status": "Succeeded", "result": result_body})]),
    )
    assert result.status == "error"
    assert result.reason["reason"] == "malformed_response"


@pytest.mark.parametrize(
    "result",
    [
        {"analyzerId": "a"},
        {"contents": "x"},
        {"contents": ["x"]},
        {"contents": [{"kind": "document", "markdown": 5, "startPageNumber": 1, "endPageNumber": 1}]},
        {"contents": [{"kind": "document", "startPageNumber": 2, "endPageNumber": 1}]},
        {"contents": [{"kind": "document", "startPageNumber": 1.0, "endPageNumber": 1}]},
        {"contents": [{"kind": "document"}]},
    ],
)
def test_parse_result_contents_rejects_malformed_results(result: dict[str, Any]) -> None:
    # ``@trust_boundary`` honesty proof for ``parse_result_contents``'s ``result`` parameter.
    with pytest.raises(MalformedResponseError):
        parse_result_contents(result)


def test_parse_result_contents_audio_contents_carry_no_pages() -> None:
    parsed = parse_result_contents({"contents": [{"kind": "audioVisual", "markdown": "speech", "startTimeMs": 0, "endTimeMs": 5}]})
    assert (len(parsed), parsed.page_count, parsed.markdown()) == (1, 0, "speech")


def test_malformed_fields_fail_closed_only_when_fields_are_requested() -> None:
    bad_fields = {**_DOC, "fields": {"A": {"valueString": "no type"}}}
    ok = _run(_transform(), _FakeClient(_post_202(), [_Resp(200, body=_succeeded(bad_fields))]))
    assert ok.status == "success"
    bad = _run(_transform(fields_field="f"), _FakeClient(_post_202(), [_Resp(200, body=_succeeded(bad_fields))]))
    assert bad.reason["reason"] == "malformed_response"


def test_submit_rejection_carries_bounded_codes() -> None:
    body = {"error": {"code": "InvalidRequest", "message": "secret", "innererror": {"code": "ModelDeploymentNotFound"}}}
    result = _run(_transform(), _FakeClient(_Resp(400, body=body), []))
    assert result.reason == {
        "reason": "api_error",
        "error_type": "submit_rejected",
        "status_code": 400,
        "code": "InvalidRequest",
        "cause": "ModelDeploymentNotFound",
    }


def test_operation_location_missing_and_untrusted() -> None:
    assert _run(_transform(), _FakeClient(_Resp(202), [])).reason == {"reason": "operation_location_missing"}
    fake = _FakeClient(_Resp(202, headers={"operation-location": "https://evil.example/contentunderstanding/analyzerResults/x"}), [])
    assert _run(_transform(), fake).reason == {"reason": "operation_location_untrusted"}
    assert fake.gets == []  # the api-key is never sent to the foreign host


def test_capacity_retry_honours_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _transform()
    clock = [0.0]
    waits: list[float] = []

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        clock[0] += timeout
        return False

    monkeypatch.setattr(analyze_lro_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(t._shutdown, "wait", wait)
    fake = _FakeClient([_Resp(429, headers={"retry-after": "4"}), _post_202()], [_Resp(200, body=_succeeded(_DOC))])
    with t._http_clients_lock:
        t._http_clients["s1"] = fake
    try:
        result = t._process_single_with_state(_row(), "s1", ctx=make_context(), token_id=None)
    finally:
        with t._http_clients_lock:
            t._http_clients.pop("s1", None)
    assert result.status == "success"
    assert waits[0] == 4.0


def test_poll_timeout() -> None:
    t = _transform()
    t._poll_timeout_seconds = -1.0
    result = _run(t, _FakeClient(_post_202(), [_Resp(200, body={"id": "op", "status": "Running"})]))
    assert result.reason["reason"] == "poll_timeout"


def test_reported_analyzer_id_that_is_not_an_id_is_not_echoed() -> None:
    body = _succeeded(_DOC)
    body["result"]["analyzerId"] = "<b>" * 30
    result = _run(_transform(), _FakeClient(_post_202(), [_Resp(200, body=body)]))
    assert result.success_reason["metadata"]["analyzer_id"] == "prebuilt-invoice"


# ── Real AuditedHTTPClient through respx ───────────────────────────────────


@respx.mock
def test_real_client_lro_round_trip() -> None:
    t = _transform(fields_field="cu_fields")
    recorder = Mock(spec=ExecutionRepository)
    recorder.record_call = Mock(spec_set=lambda *args, **kwargs: None)
    t._recorder = recorder
    t._run_id = "run-1"
    events: list[object] = []
    t._telemetry_emit = events.append
    t._poll_interval_seconds = 0.0
    t._poll_max_interval_seconds = 0.0
    respx.post(url__regex=r".*/contentunderstanding/analyzers/prebuilt-invoice:analyze.*").mock(
        return_value=httpx.Response(202, headers={"operation-location": OP})
    )
    respx.get(OP).mock(return_value=httpx.Response(200, json=_succeeded(_DOC)))

    result = t._process_single_with_state(_row(), "s-real", ctx=make_context(run_id="run-1"), token_id=None)
    try:
        assert result.status == "success"
        assert result.row.to_dict()["cu_fields"]["VendorName"] == "Contoso"
        post = respx.calls[0].request
        assert post.headers["Ocp-Apim-Subscription-Key"] == "k"
        assert json.loads(post.content) == {"inputs": [{"url": "https://x/y.pdf"}]}
        assert "api-version=2025-11-01" in str(post.url)
        assert respx.calls[-1].request.method == "GET"
        assert [e for e in events if isinstance(e, ExternalCallCompleted) and e.call_type == CallType.HTTP]
        assert recorder.record_call.call_count == 2
    finally:
        with t._http_clients_lock:
            client = t._http_clients.pop("s-real", None)
        if client is not None:
            client.close()
