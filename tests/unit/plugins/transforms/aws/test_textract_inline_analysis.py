"""Unit tests for the Amazon Textract inline-analysis transform."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts import AuditCharacteristic, Determinism
from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws.textract_client import (
    InlineAnalysisResult,
    TextractResponseError,
    TextractServiceError,
)
from elspeth.plugins.transforms.aws.textract_inline_analysis import (
    AWSTextractInlineAnalysis,
    AWSTextractInlineAnalysisConfig,
)
from elspeth.testing import make_pipeline_row

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"inline-analysis-document"
_PNG_SHA256 = hashlib.sha256(_PNG_BYTES).hexdigest()
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-document"
_PDF_BYTES = b"%PDF-1.7\n" + b"pdf-document"


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "region": "ap-southeast-2",
        "document_format": "png",
        "feature_types": ["FORMS", "TABLES"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


def _load(**overrides: object) -> AWSTextractInlineAnalysisConfig:
    return AWSTextractInlineAnalysisConfig.from_dict(
        _config(**overrides),
        plugin_name="aws_textract_inline_analysis",
    )


# --- configuration contract -------------------------------------------------


def test_minimal_default_chain_config() -> None:
    cfg = _load()

    assert cfg.auth_mode == "default_chain"
    assert cfg.document_format == "png"
    assert cfg.blob_ref_field == "blob_ref"
    assert cfg.all_output_field_names() == ["textract_text"]
    # Deliberate authority decision (design 2026-07-29): the inline plugin is
    # web-authorable directly; credentials flow as secret_ref markers through
    # WebSecretService, and the deployment's plugin-authorization allowlist is
    # the operator control point.
    assert AWSTextractInlineAnalysis.web_config_authority is WebConfigAuthority.USER_CONFIGURABLE


def test_document_format_is_required() -> None:
    with pytest.raises(PluginConfigError, match="document_format"):
        _load(document_format=None)


@pytest.mark.parametrize("document_format", ["jpg", "tiff", "PNG", ""])
def test_unknown_document_format_fails(document_format: str) -> None:
    with pytest.raises(PluginConfigError, match="document_format"):
        _load(document_format=document_format)


def test_secret_refs_mode_requires_access_and_secret_key() -> None:
    with pytest.raises(PluginConfigError, match="required together"):
        _load(auth_mode="secret_refs", aws_access_key_id="resolved-access-id")


def test_secret_refs_mode_accepts_pair_and_optional_session_token() -> None:
    cfg = _load(
        auth_mode="secret_refs",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        aws_session_token="resolved-session-token",
    )

    assert cfg.auth_mode == "secret_refs"
    assert cfg.aws_session_token == "resolved-session-token"


def test_default_chain_rejects_explicit_credentials() -> None:
    with pytest.raises(PluginConfigError, match="forbidden in default_chain"):
        _load(
            aws_access_key_id="resolved-access-id",
            aws_secret_access_key="resolved-secret-key",
        )


def test_session_token_without_credential_pair_fails() -> None:
    with pytest.raises(PluginConfigError, match="credential pair"):
        _load(auth_mode="secret_refs", aws_session_token="resolved-session-token")


def test_queries_require_queries_feature() -> None:
    with pytest.raises(PluginConfigError, match="QUERIES"):
        _load(queries=[{"text": "What is the total?"}])


def test_queries_feature_requires_queries() -> None:
    with pytest.raises(PluginConfigError, match="QUERIES"):
        _load(feature_types=["QUERIES"])


@pytest.mark.parametrize("pages", [[], ["1"], ["*"]])
def test_single_page_query_selectors_accepted(pages: list[str]) -> None:
    cfg = _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": pages}])

    assert cfg.queries[0].pages == pages


@pytest.mark.parametrize("pages", [["2"], ["1-2"], ["1", "2"], ["1-*"]])
def test_multi_page_query_selectors_rejected(pages: list[str]) -> None:
    with pytest.raises(PluginConfigError, match="single-page"):
        _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": pages}])


def test_query_count_is_capped_at_fifteen() -> None:
    queries = [{"text": f"Question {index}?"} for index in range(16)]
    with pytest.raises(PluginConfigError, match="queries"):
        _load(feature_types=["QUERIES"], queries=queries)


def test_duplicate_output_names_fail() -> None:
    with pytest.raises(PluginConfigError, match="Duplicate output field"):
        _load(text_field="result", metadata_field="result")


def test_at_least_one_output_is_required() -> None:
    with pytest.raises(PluginConfigError, match="At least one output target"):
        _load(text_field=None)


@pytest.mark.parametrize("feature", ["TABLE", "forms", "OCR", ""])
def test_unknown_feature_type_fails(feature: str) -> None:
    with pytest.raises(PluginConfigError, match="feature_types"):
        _load(feature_types=[feature])


def test_duplicate_feature_types_fail() -> None:
    with pytest.raises(PluginConfigError, match="duplicates"):
        _load(feature_types=["TABLES", "FORMS", "TABLES"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "ap_southeast_2"),
        ("blob_ref_field", "   "),
        ("text_field", ""),
        ("page_count_field", " "),
        ("metadata_field", "\t"),
        ("result_field", "\n"),
    ],
)
def test_invalid_region_or_field_name_fails(field: str, value: object) -> None:
    with pytest.raises(PluginConfigError):
        _load(**{field: value})


@pytest.mark.parametrize("region", ["af-south-1", "cn-north-1", "moon-east-1"])
def test_rejects_unsupported_region(region: str) -> None:
    with pytest.raises(PluginConfigError, match="supported Amazon Textract region"):
        _load(region=region)


def test_max_document_bytes_cannot_exceed_provider_bound() -> None:
    with pytest.raises(PluginConfigError, match="max_document_bytes"):
        _load(max_document_bytes=BINARY_DOCUMENT_MAX_BYTES + 1)


@pytest.mark.parametrize(
    "field",
    ["max_document_bytes", "max_blocks", "max_result_bytes", "request_timeout_seconds", "batch_wait_timeout_seconds"],
)
def test_positive_bounds_reject_zero(field: str) -> None:
    with pytest.raises(PluginConfigError, match=field):
        _load(**{field: 0})


@pytest.mark.parametrize("field", ["request_timeout_seconds", "batch_wait_timeout_seconds"])
def test_non_finite_timing_configuration_fails(field: str) -> None:
    with pytest.raises(PluginConfigError, match=field):
        _load(**{field: float("inf")})


def test_batch_wait_covers_every_sdk_attempt_plus_headroom() -> None:
    transform = AWSTextractInlineAnalysis(
        _config(
            request_timeout_seconds=100.0,
            batch_wait_timeout_seconds=1.0,
        )
    )

    # 3 attempts * (100s read + 10s connect) + 90s headroom.
    assert transform._effective_batch_wait_timeout_seconds == 420.0


def test_default_batch_wait_is_raised_to_cover_sdk_retries() -> None:
    transform = AWSTextractInlineAnalysis(_config())

    # Defaults: 3 attempts * (120s read + 10s connect) + 90s > the 420s knob.
    assert transform._effective_batch_wait_timeout_seconds == 480.0


def test_full_configuration_declares_inputs_and_ordered_outputs() -> None:
    cfg = _load(
        auth_mode="secret_refs",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        blob_ref_field="document_ref",
        feature_types=["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"],
        queries=[{"text": "What is the total?", "alias": "invoice_total", "pages": ["1"]}],
        page_count_field="textract_page_count",
        metadata_field="textract_metadata",
        result_field="textract_native",
        extract={
            "pages": "textract_pages",
            "tables": "textract_tables",
            "forms": "textract_forms",
            "queries": "textract_queries",
            "signatures": "textract_signatures",
            "layout": "textract_layout",
        },
    )

    assert cfg.declared_input_fields == frozenset({"document_ref"})
    assert cfg.all_output_field_names() == [
        "textract_text",
        "textract_page_count",
        "textract_metadata",
        "textract_native",
        "textract_pages",
        "textract_tables",
        "textract_forms",
        "textract_queries",
        "textract_signatures",
        "textract_layout",
    ]


def test_blob_ref_field_naming_an_output_target_is_rejected() -> None:
    with pytest.raises(PluginConfigError, match="blob_ref_field"):
        AWSTextractInlineAnalysis(_config(blob_ref_field="textract_text"))


# --- behavior harness -------------------------------------------------------


def _analyze_response(
    *,
    blocks: list[dict[str, object]] | None = None,
    page_count: int = 1,
    model: str = "1.0",
) -> dict[str, Any]:
    return {
        "DocumentMetadata": {"Pages": page_count},
        "AnalyzeDocumentModelVersion": model,
        "Blocks": blocks if blocks is not None else _basic_blocks(),
    }


def _basic_blocks(*, page: int = 1, text: str = "Document text") -> list[dict[str, object]]:
    return [
        {"BlockType": "PAGE", "Id": f"page-{page}", "Page": page},
        {
            "BlockType": "LINE",
            "Id": f"line-{page}",
            "Page": page,
            "Text": text,
            "Confidence": 99.0,
        },
    ]


class FakeInlineClient:
    def __init__(self, *, result: InlineAnalysisResult | Exception | None = None) -> None:
        self.result = InlineAnalysisResult(semantic_response=_analyze_response(), sdk_attempts=1) if result is None else result
        self.calls: list[dict[str, object]] = []

    def analyze_document(self, **kwargs: object) -> InlineAnalysisResult:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakePayloadStore:
    def __init__(self, contents: dict[str, bytes] | None = None, *, integrity_error: bool = False) -> None:
        self.contents = {_PNG_SHA256: _PNG_BYTES} if contents is None else contents
        self.integrity_error = integrity_error
        self.retrieve_calls: list[str] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("the transform must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.retrieve_calls.append(content_hash)
        if self.integrity_error:
            raise IntegrityError("payload integrity check failed")
        try:
            return self.contents[content_hash]
        except KeyError:
            raise PayloadNotFoundError(content_hash) from None

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.contents

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("the transform must never delete payloads")


def _row(**overrides: object) -> PipelineRow:
    data: dict[str, object] = {"blob_ref": _PNG_SHA256}
    data.update(overrides)
    return make_pipeline_row(data)


def _transform_for_client(
    client: FakeInlineClient,
    *,
    store: FakePayloadStore | None = None,
    **overrides: object,
) -> AWSTextractInlineAnalysis:
    transform = AWSTextractInlineAnalysis(_config(**overrides))
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    transform._payload_store = store if store is not None else FakePayloadStore()
    transform._row_clients["state-1"] = client  # type: ignore[assignment]
    return transform


def _run(transform: AWSTextractInlineAnalysis, row: PipelineRow | None = None):
    return transform._process_single_with_state(
        _row() if row is None else row,
        "state-1",
        token_id="token-1",
    )


# --- transform metadata -----------------------------------------------------


def test_transform_metadata_and_declared_fields() -> None:
    transform = AWSTextractInlineAnalysis(_config(page_count_field="textract_pages", extract={"tables": "textract_tables"}))

    assert transform.name == "aws_textract_inline_analysis"
    assert transform.determinism is Determinism.EXTERNAL_CALL
    assert transform.passes_through_input is True
    assert transform.creates_tokens is False
    assert transform.audit_characteristics == frozenset({AuditCharacteristic.CREDENTIALS})
    assert transform.declared_input_fields == frozenset({"blob_ref"})
    assert transform.declared_output_fields == frozenset({"textract_text", "textract_pages", "textract_tables"})
    assert transform._feature_types == ("FORMS", "TABLES")
    assert transform._sdk_client is None


def test_probe_config_instantiates_without_network() -> None:
    AWSTextractInlineAnalysis(AWSTextractInlineAnalysis.probe_config())


def test_process_raises_use_accept() -> None:
    transform = AWSTextractInlineAnalysis(_config())

    with pytest.raises(NotImplementedError, match="accept"):
        transform.process(_row(), SimpleNamespace())


# --- per-row data flow ------------------------------------------------------


def test_successful_analysis_enriches_row_with_billing_evidence() -> None:
    client = FakeInlineClient(result=InlineAnalysisResult(semantic_response=_analyze_response(), sdk_attempts=2))
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "success"
    assert result.row["textract_text"] == "Document text"
    assert result.row["blob_ref"] == _PNG_SHA256
    metadata = result.success_reason["metadata"]
    assert metadata["document_sha256"] == _PNG_SHA256
    assert metadata["document_size_bytes"] == len(_PNG_BYTES)
    assert metadata["document_format"] == "png"
    assert metadata["page_count"] == 1
    assert metadata["model_version"] == "1.0"
    assert metadata["sdk_attempts"] == 2
    assert metadata["result_status"] == "succeeded"


def test_analyze_receives_exact_bytes_hash_and_sorted_features() -> None:
    client = FakeInlineClient()
    transform = _transform_for_client(
        client,
        feature_types=["QUERIES", "TABLES"],
        queries=[{"text": "Total?", "alias": "total", "pages": ["1"]}],
    )

    result = _run(transform)

    assert result.status == "success"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["document_bytes"] is _PNG_BYTES
    assert call["document_sha256"] == _PNG_SHA256
    assert call["document_format"] == "png"
    assert call["feature_types"] == ("QUERIES", "TABLES")
    assert [dict(query) for query in call["queries"]] == [{"text": "Total?", "alias": "total", "pages": ("1",)}]


def test_all_normalized_facet_outputs_are_projected() -> None:
    client = FakeInlineClient()
    transform = _transform_for_client(
        client,
        page_count_field="textract_page_count",
        metadata_field="textract_metadata",
        result_field="textract_native",
        extract={
            "pages": "textract_pages",
            "tables": "textract_tables",
            "forms": "textract_forms",
            "signatures": "textract_signatures",
            "layout": "textract_layout",
        },
    )

    result = _run(transform)

    assert result.status == "success"
    assert result.row["textract_page_count"] == 1
    assert deep_thaw(result.row["textract_metadata"]) == {
        "document_sha256": _PNG_SHA256,
        "document_size_bytes": len(_PNG_BYTES),
        "document_format": "png",
        "page_count": 1,
        "block_count": 2,
        "model_version": "1.0",
        "feature_types": ["FORMS", "TABLES"],
    }
    native = result.row["textract_native"]
    assert deep_thaw(native["DocumentMetadata"]) == {"Pages": 1}
    assert native["AnalyzeDocumentModelVersion"] == "1.0"
    assert len(native["Blocks"]) == 2
    assert result.row["textract_pages"][0]["page"] == 1
    assert deep_thaw(result.row["textract_tables"]) == []
    assert deep_thaw(result.row["textract_forms"]) == []
    assert deep_thaw(result.row["textract_signatures"]) == []
    assert deep_thaw(result.row["textract_layout"]) == []


def test_missing_blob_ref_field_fails_before_any_retrieval() -> None:
    client = FakeInlineClient()
    store = FakePayloadStore()
    transform = _transform_for_client(client, store=store)

    result = _run(transform, make_pipeline_row({"other": "value"}))

    assert result.status == "error"
    assert result.reason == {"reason": "missing_field", "field": "blob_ref"}
    assert store.retrieve_calls == []
    assert client.calls == []


def test_non_string_blob_ref_fails() -> None:
    transform = _transform_for_client(FakeInlineClient())

    result = _run(transform, _row(blob_ref=42))

    assert result.status == "error"
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["actual_type"] == "int"


@pytest.mark.parametrize("payload_ref", ["A" * 64, "a" * 63, "a" * 65, "zz", ""])
def test_malformed_payload_ref_fails_before_retrieval(payload_ref: str) -> None:
    store = FakePayloadStore()
    transform = _transform_for_client(FakeInlineClient(), store=store)

    result = _run(transform, _row(blob_ref=payload_ref))

    assert result.status == "error"
    assert result.reason["error_type"] == "invalid_payload_ref"
    assert store.retrieve_calls == []


def test_missing_payload_is_a_row_error() -> None:
    store = FakePayloadStore(contents={})
    transform = _transform_for_client(FakeInlineClient(), store=store)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "blob_not_found"
    assert result.reason["blob_ref"] == _PNG_SHA256


def test_payload_integrity_corruption_propagates_as_infrastructure_failure() -> None:
    store = FakePayloadStore(integrity_error=True)
    transform = _transform_for_client(FakeInlineClient(), store=store)

    with pytest.raises(IntegrityError):
        _run(transform)


def test_empty_document_fails() -> None:
    empty_sha = hashlib.sha256(b"").hexdigest()
    store = FakePayloadStore(contents={empty_sha: b""})
    transform = _transform_for_client(FakeInlineClient(), store=store)

    result = _run(transform, _row(blob_ref=empty_sha))

    assert result.status == "error"
    assert result.reason["error_type"] == "empty_document"


def test_oversized_document_fails_before_the_provider_call() -> None:
    client = FakeInlineClient()
    transform = _transform_for_client(client, max_document_bytes=8)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 8
    assert client.calls == []


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (_JPEG_BYTES, "different known signature"),
        (b" " + _PNG_BYTES, "leading whitespace"),
        (b"\xef\xbb\xbf" + _PNG_BYTES, "byte-order mark"),
        (b"prefix" + _PNG_BYTES, "embedded signature later"),
    ],
)
def test_signature_mismatch_fails_closed(content: bytes, label: str) -> None:
    del label
    content_sha = hashlib.sha256(content).hexdigest()
    store = FakePayloadStore(contents={content_sha: content})
    client = FakeInlineClient()
    transform = _transform_for_client(client, store=store)

    result = _run(transform, _row(blob_ref=content_sha))

    assert result.status == "error"
    assert result.reason["error_type"] == "document_signature_mismatch"
    assert result.reason["expected"] == "png"
    assert client.calls == []


@pytest.mark.parametrize(
    ("document_format", "content"),
    [("png", _PNG_BYTES), ("jpeg", _JPEG_BYTES), ("pdf", _PDF_BYTES)],
)
def test_every_declared_format_signature_is_accepted(document_format: str, content: bytes) -> None:
    content_sha = hashlib.sha256(content).hexdigest()
    store = FakePayloadStore(contents={content_sha: content})
    transform = _transform_for_client(FakeInlineClient(), store=store, document_format=document_format)

    result = _run(transform, _row(blob_ref=content_sha))

    assert result.status == "success"


@pytest.mark.parametrize("retryable", [True, False])
def test_service_error_preserves_retryability(retryable: bool) -> None:
    client = FakeInlineClient(
        result=TextractServiceError(code="ThrottlingException" if retryable else "AccessDeniedException", retryable=retryable)
    )
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "analysis_failed"
    assert result.reason["error_type"] == "service_error"
    assert result.retryable is retryable


@pytest.mark.parametrize(
    ("category", "reason"),
    [("response_too_large", "result_too_large"), ("malformed_response", "malformed_response")],
)
def test_client_response_error_maps_to_bounded_reasons(category: str, reason: str) -> None:
    client = FakeInlineClient(result=TextractResponseError(category=category))
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == reason
    assert result.retryable is False


def test_dangling_relationship_fails_as_malformed_response() -> None:
    blocks = [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
        {
            "BlockType": "LINE",
            "Id": "line-1",
            "Page": 1,
            "Text": "text",
            "Confidence": 99.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["missing-block"]}],
        },
    ]
    client = FakeInlineClient(result=InlineAnalysisResult(semantic_response=_analyze_response(blocks=blocks), sdk_attempts=1))
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "malformed_response"
    assert result.reason["error_type"] == "result_validation"


def test_block_count_over_limit_fails_as_too_many_blocks() -> None:
    client = FakeInlineClient()
    transform = _transform_for_client(client, max_blocks=1)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "too_many_blocks"


# Also the pdf_rasterize case: a rasterized page whose response reports Pages != 1 must
# still fail — the guard validates the provider RESPONSE and is not relaxed by the exploder.
def test_multi_page_response_fails_page_count_policy() -> None:
    blocks = [*_basic_blocks(page=1), {"BlockType": "PAGE", "Id": "page-2", "Page": 2}]
    client = FakeInlineClient(result=InlineAnalysisResult(semantic_response=_analyze_response(blocks=blocks, page_count=2), sdk_attempts=1))
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason == {
        "reason": "validation_failed",
        "error_type": "page_count",
        "expected": "1",
        "actual": "2",
    }


def test_human_loop_activation_output_is_malformed() -> None:
    response = _analyze_response()
    response["HumanLoopActivationOutput"] = {"HumanLoopArn": "arn:aws:..."}
    client = FakeInlineClient(result=InlineAnalysisResult(semantic_response=response, sdk_attempts=1))
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "malformed_response"


def test_shutdown_requested_skips_the_provider_call() -> None:
    client = FakeInlineClient()
    transform = _transform_for_client(client)
    transform._shutdown.set()

    result = _run(transform)

    assert result.status == "error"
    assert result.reason == {"reason": "shutdown_requested"}
    assert client.calls == []


def test_no_document_bytes_leak_into_reasons_or_success_metadata() -> None:
    success_transform = _transform_for_client(FakeInlineClient())
    success = _run(success_transform)
    failure_transform = _transform_for_client(FakeInlineClient(result=TextractServiceError(code="BadDocumentException", retryable=False)))
    failure = _run(failure_transform)

    assert b"inline-analysis-document" not in repr(success.success_reason).encode("utf-8", "ignore")
    assert "inline-analysis-document" not in repr(success.success_reason)
    assert "inline-analysis-document" not in repr(failure.reason)


# --- lifecycle --------------------------------------------------------------


def test_on_start_requires_landscape() -> None:
    transform = AWSTextractInlineAnalysis(_config())
    ctx = SimpleNamespace(
        landscape=None,
        node_id="node-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        rate_limit_registry=None,
        shutdown_event=None,
        payload_store=FakePayloadStore(),
    )

    with pytest.raises(FrameworkBugError, match="Landscape"):
        transform.on_start(ctx)


def test_on_start_builds_sdk_with_resolved_secrets_and_close_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = SimpleNamespace(close_count=0)

    def close_sdk() -> None:
        sdk.close_count += 1

    sdk.close = close_sdk
    captured: dict[str, object] = {}

    def build_sdk(**kwargs: object) -> object:
        captured.update(kwargs)
        return sdk

    limiter = object()
    registry = SimpleNamespace(get_limiter=lambda name: limiter if name == "aws_textract_inline_analysis" else None)
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_inline_analysis.build_textract_sync_sdk_client",
        build_sdk,
    )
    store = FakePayloadStore()
    transform = AWSTextractInlineAnalysis(
        _config(
            auth_mode="secret_refs",
            aws_access_key_id="resolved-access-id",
            aws_secret_access_key="resolved-secret-key",
            aws_session_token="resolved-session-token",
            request_timeout_seconds=60.0,
        )
    )
    ctx = SimpleNamespace(
        landscape=object(),
        node_id="node-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        rate_limit_registry=registry,
        shutdown_event=None,
        payload_store=store,
    )

    transform.on_start(ctx)

    assert captured == {
        "region": "ap-southeast-2",
        "aws_access_key_id": "resolved-access-id",
        "aws_secret_access_key": "resolved-secret-key",
        "aws_session_token": "resolved-session-token",
        "read_timeout": 60.0,
    }
    assert transform._limiter is limiter
    assert transform._payload_store is store
    transform.close()
    transform.close()
    assert sdk.close_count == 1


def test_missing_payload_store_is_a_framework_bug_at_row_time() -> None:
    transform = _transform_for_client(FakeInlineClient())
    transform._payload_store = None

    with pytest.raises(FrameworkBugError, match="payload store"):
        _run(transform)


# --- invariant probe --------------------------------------------------------


class _ProbeRecorder:
    def allocate_call_index(self, state_id: str) -> int:
        del state_id
        return 0

    def record_call(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(id="call-probe")


def test_forward_invariant_probe_runs_the_production_path_offline() -> None:
    transform = AWSTextractInlineAnalysis(_config())
    probe_rows = transform.forward_invariant_probe_rows(make_pipeline_row({"seed": "value"}))
    assert probe_rows[0]["blob_ref"] is not None
    ctx = SimpleNamespace(
        landscape=_ProbeRecorder(),
        run_id="run-probe",
        telemetry_emit=lambda _event: None,
        state_id=None,
        token=None,
    )

    result = transform.execute_forward_invariant_probe(probe_rows, ctx)

    assert result.status == "success"
    assert result.row["textract_text"] == "probe"
    # The probe must leave no live state behind.
    assert transform._sdk_client is None
    assert transform._payload_store is None
    assert transform._row_clients == {}


def test_assistance_names_the_authority_boundaries_and_the_multipage_on_ramp() -> None:
    assistance = AWSTextractInlineAnalysis.get_agent_assistance()
    assert assistance is not None
    hints = " ".join(assistance.composer_hints)
    for required in ("blob_rows", "jpeg", "single-page", "aws_textract_document_analysis", "billable"):
        assert required in hints
    # A multipage PDF reaches this plugin only through pdf_rasterize; the hint must say so
    # AND name the two options the downstream node must set (a declaration test pins
    # existence, a claim test pins the advice).
    on_ramp = [hint for hint in assistance.composer_hints if "pdf_rasterize" in hint]
    assert len(on_ramp) == 1
    assert "blob_ref_field: page_blob_ref" in on_ramp[0] and "document_format: png" in on_ramp[0]
    assert AWSTextractInlineAnalysis.get_agent_assistance(issue_code="anything") is None
