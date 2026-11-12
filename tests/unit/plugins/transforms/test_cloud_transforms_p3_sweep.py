"""Regression tests for the P3 cloud-transform sweep findings (20261111-plugins-review).

Each section names the sweep finding it pins. The tests drive the production
code paths the finding describes and assert on the observable outcome.
"""

from __future__ import annotations

import hashlib
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from elspeth.contracts import RunMode, TransformResult
from elspeth.contracts.errors import PluginRetryableError
from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient, GuardrailServiceError
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysis
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_analyze_document_result,
)
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.mock_audit import mock_item_audit_authority
from tests.unit.plugins.transforms.azure.test_document_intelligence import _FakeClient as _DIFakeClient
from tests.unit.plugins.transforms.azure.test_document_intelligence import _post_202 as _di_post_202
from tests.unit.plugins.transforms.azure.test_document_intelligence import _Resp as _DIResp
from tests.unit.plugins.transforms.azure.test_document_intelligence import _run_with_fake as _di_run_with_fake
from tests.unit.plugins.transforms.azure.test_document_intelligence import _t_for_lro as _di_transform

# ---------------------------------------------------------------------------
# Finding #35: FORMS projection must carry checkbox selection state.
# ---------------------------------------------------------------------------


def _geometry() -> dict[str, Any]:
    return {
        "BoundingBox": {"Width": 0.5, "Height": 0.1, "Left": 0.1, "Top": 0.2},
        "Polygon": [
            {"X": 0.1, "Y": 0.2},
            {"X": 0.6, "Y": 0.2},
            {"X": 0.6, "Y": 0.3},
            {"X": 0.1, "Y": 0.3},
        ],
    }


def _checkbox_form_response(*statuses: str) -> dict[str, Any]:
    """One page holding one KEY/VALUE pair per status; each VALUE's CHILD is a checkbox."""
    blocks: list[dict[str, Any]] = [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1, "Geometry": _geometry()},
    ]
    for index, status in enumerate(statuses):
        blocks.extend(
            [
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"key-{index}",
                    "Page": 1,
                    "EntityTypes": ["KEY"],
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                    "Relationships": [
                        {"Type": "CHILD", "Ids": [f"key-word-{index}"]},
                        {"Type": "VALUE", "Ids": [f"value-{index}"]},
                    ],
                },
                {
                    "BlockType": "WORD",
                    "Id": f"key-word-{index}",
                    "Page": 1,
                    "Text": f"Option {index}",
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                },
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"value-{index}",
                    "Page": 1,
                    "EntityTypes": ["VALUE"],
                    "Confidence": 94.0,
                    "Geometry": _geometry(),
                    "Relationships": [{"Type": "CHILD", "Ids": [f"selection-{index}"]}],
                },
                {
                    "BlockType": "SELECTION_ELEMENT",
                    "Id": f"selection-{index}",
                    "Page": 1,
                    "SelectionStatus": status,
                    "Confidence": 93.0,
                    "Geometry": _geometry(),
                },
            ]
        )
    return {"DocumentMetadata": {"Pages": 1}, "AnalyzeDocumentModelVersion": "1.0", "Blocks": blocks}


def _normalize_forms(response: dict[str, Any]) -> Any:
    return normalize_analyze_document_result(
        response=response,
        feature_types=("FORMS",),
        max_blocks=1_000,
        max_result_bytes=1_000_000,
    )


def test_f35_forms_projection_distinguishes_checked_from_unchecked_checkbox() -> None:
    result = _normalize_forms(_checkbox_form_response("SELECTED", "NOT_SELECTED"))

    forms = list(result.forms)
    assert len(forms) == 2
    assert all("value_selection_status" in form for form in forms), [sorted(form) for form in forms]
    statuses = {form["key"]: form["value_selection_status"] for form in forms}
    assert statuses == {"Option 0": "SELECTED", "Option 1": "NOT_SELECTED"}


def test_f35_forms_projection_rejects_unknown_checkbox_status() -> None:
    with pytest.raises(MalformedTextractResponse, match="SELECTED or NOT_SELECTED"):
        _normalize_forms(_checkbox_form_response("MAYBE"))


# ---------------------------------------------------------------------------
# Finding #30: inline Textract must apply max_document_bytes while reading.
# ---------------------------------------------------------------------------

_F30_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 4096
_F30_SHA = hashlib.sha256(_F30_PNG).hexdigest()


class _BoundedReadStore:
    """PayloadStore double that records full and bounded reads separately."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.full_reads: list[str] = []
        self.bounded_reads: list[tuple[str, int]] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("the transform must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.full_reads.append(content_hash)
        return self.content

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.bounded_reads.append((content_hash, max_bytes))
        return None if len(self.content) > max_bytes else self.content

    def exists(self, content_hash: str) -> bool:
        return True

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("the transform must never delete payloads")


def test_f30_oversized_inline_document_is_rejected_by_a_bounded_read() -> None:
    transform = AWSTextractInlineAnalysis(
        {
            "region": "ap-southeast-2",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
            "max_document_bytes": 64,
        }
    )
    store = _BoundedReadStore(_F30_PNG)
    transform._payload_store = store

    result = transform._read_document_bytes(make_pipeline_row({"blob_ref": _F30_SHA}))

    assert isinstance(result, TransformResult)
    assert result.status == "error"
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 64
    assert store.full_reads == [], "the whole blob was read before the size cap applied"
    assert store.bounded_reads == [(_F30_SHA, 64)]
    assert result.reason["actual"] == ">64"


# ---------------------------------------------------------------------------
# Finding #36: Textract close() must not set the engine-owned shutdown event.
# ---------------------------------------------------------------------------


def _f36_document_analysis() -> AWSTextractDocumentAnalysis:
    return AWSTextractDocumentAnalysis(
        {
            "region": "ap-southeast-2",
            "bucket_field": "document_bucket",
            "key_field": "document_key",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }
    )


def _f36_inline_analysis() -> AWSTextractInlineAnalysis:
    return AWSTextractInlineAnalysis(
        {
            "region": "ap-southeast-2",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }
    )


def _f36_start(transform: AWSTextractDocumentAnalysis | AWSTextractInlineAnalysis, run_shutdown: threading.Event) -> None:
    transform.on_start(
        SimpleNamespace(
            landscape=object(),
            node_id="node-1",
            run_id="run-1",
            telemetry_emit=lambda _event: None,
            rate_limit_registry=None,
            shutdown_event=run_shutdown,
            payload_store=None,
            run_mode=RunMode.REPLAY,
        )
    )


_F36_FACTORIES = [
    pytest.param(_f36_document_analysis, id="document_analysis"),
    pytest.param(_f36_inline_analysis, id="inline_analysis"),
]


@pytest.mark.parametrize("factory", _F36_FACTORIES)
def test_f36_close_leaves_the_engine_shutdown_event_unset(factory: Any) -> None:
    run_shutdown = threading.Event()
    transform = factory()
    _f36_start(transform, run_shutdown)

    transform.close()

    assert not run_shutdown.is_set(), "plugin close() raised the run-wide cancellation signal"


@pytest.mark.parametrize("factory", _F36_FACTORIES)
def test_f36_engine_shutdown_event_still_stops_row_work(factory: Any) -> None:
    run_shutdown = threading.Event()
    transform = factory()
    _f36_start(transform, run_shutdown)
    run_shutdown.set()

    result = transform._process_single_with_state(
        make_pipeline_row({"blob_ref": _F30_SHA, "document_bucket": "bucket", "document_key": "key"}),
        "state-1",
        ctx=make_context(run_id="run-1"),
        token_id="token-1",
    )

    assert result.status == "error"
    assert result.reason == {"reason": "shutdown_requested"}
    transform.close()


# ---------------------------------------------------------------------------
# Finding #50: Guardrail transport failures must audit the observed attempts.
# ---------------------------------------------------------------------------


class _F50Execution:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def allocate_call_index(self, state_id: str, *, member_token: Any, work_item: Any) -> int:
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"call-{len(self.calls)}")


def test_f50_transport_failure_audits_every_botocore_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import EndpointConnectionError
    from botocore.retries.standard import ExponentialBackoff

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(ExponentialBackoff, "delay_amount", lambda self, context: 0)

    execution = _F50Execution()
    client = BedrockGuardrailsClient(
        **mock_item_audit_authority("run-1"),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        guardrail_identifier="privateguardrail",
        guardrail_version="7",
        region="us-east-1",
        audit_salt=b"0123456789abcdef0123456789abcdef",
        token_id="token-1",
    )
    sends: list[str] = []

    def unreachable_endpoint(request: Any, **_kwargs: Any) -> None:
        sends.append(request.url)
        raise EndpointConnectionError(endpoint_url=request.url)

    # Registered after the client's own hooks, so botocore's real standard
    # retry handler sees an HTTPClientError on every attempt.
    client.sdk_client.meta.events.register("before-send.bedrock-runtime", unreachable_endpoint)

    with pytest.raises(GuardrailServiceError):
        client.apply_guardrail(text="marker", source="INPUT", required_filters=("PROMPT_ATTACK",))

    assert len(sends) == 3, "botocore standard retries must have made three HTTP attempts"
    assert len(execution.calls) == 1
    assert execution.calls[0]["response_data"].to_dict()["attempts"] == len(sends)


# ---------------------------------------------------------------------------
# Finding #61: a poll-phase network error must not resubmit the document.
# ---------------------------------------------------------------------------


class _F61Client(_DIFakeClient):
    """Scripted DI client whose scripted entries may be exceptions to raise."""

    def post(self, url: str, *, json: Any = None, audit_json: Any = None, timeout: float | None = None) -> Any:
        response = super().post(url, json=json, audit_json=audit_json, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, *, timeout: float | None = None) -> Any:
        response = super().get(url, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response


def _f61_succeeded() -> _DIResp:
    return _DIResp(200, body={"status": "succeeded", "analyzeResult": {"modelId": "prebuilt-layout", "content": "# Doc"}})


def _f61_connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection dropped", request=httpx.Request("GET", "https://x"))


def test_f61_poll_network_error_is_retried_in_place_without_resubmitting() -> None:
    transform = _di_transform()
    client = _F61Client(_di_post_202(), [_f61_connect_error(), _DIResp(200, body={"status": "running"}), _f61_succeeded()])

    escalated: PluginRetryableError | None = None
    result = None
    try:
        result = _di_run_with_fake(transform, client)
    except PluginRetryableError as error:
        escalated = error

    assert escalated is None, f"a poll GET network error escalated to a row retry (resubmit): {escalated}"
    assert result is not None
    assert result.status == "success"
    assert len(client.posts) == 1
    assert client.get_calls == 3


def test_f61_submit_network_error_still_escalates_to_a_row_retry() -> None:
    transform = _di_transform()
    client = _F61Client([_f61_connect_error()], [])

    with pytest.raises(PluginRetryableError):
        _di_run_with_fake(transform, client)
    assert client.get_calls == 0
