"""Unit tests for the Amazon Textract document-analysis transform."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from elspeth.contracts import AuditCharacteristic, Determinism
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws.textract_client import (
    AnalysisResultPage,
    StartAnalysisReceipt,
    TextractIdempotencyInvariantError,
    TextractResponseError,
    TextractServiceError,
)
from elspeth.plugins.transforms.aws.textract_document_analysis import (
    AWSTextractDocumentAnalysis,
    AWSTextractDocumentAnalysisConfig,
)
from elspeth.testing import make_pipeline_row


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "region": "ap-southeast-2",
        "bucket_field": "document_bucket",
        "key_field": "document_key",
        "feature_types": ["FORMS", "TABLES"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


def _load(**overrides: object) -> AWSTextractDocumentAnalysisConfig:
    return AWSTextractDocumentAnalysisConfig.from_dict(
        _config(**overrides),
        plugin_name="aws_textract_document_analysis",
    )


def test_minimal_default_chain_config() -> None:
    cfg = _load()

    assert cfg.auth_mode == "default_chain"
    assert cfg.feature_types == ["FORMS", "TABLES"]
    assert cfg.all_output_field_names() == ["textract_text"]


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


def test_duplicate_feature_types_fail_without_reordering() -> None:
    with pytest.raises(PluginConfigError, match="duplicates"):
        _load(feature_types=["TABLES", "FORMS", "TABLES"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "ap_southeast_2"),
        ("bucket_field", "   "),
        ("key_field", "\t"),
        ("version_field", "\n"),
        ("text_field", ""),
        ("page_count_field", " "),
        ("metadata_field", "\t"),
        ("result_field", "\n"),
    ],
)
def test_invalid_region_or_field_name_fails(field: str, value: object) -> None:
    with pytest.raises(PluginConfigError):
        _load(**{field: value})


@pytest.mark.parametrize(
    "selector",
    ["0", "0-2", "2-1", "1-0", "*-2", "1--2", "abcdefghij"],
)
def test_invalid_query_page_selector_fails(selector: str) -> None:
    with pytest.raises(PluginConfigError, match="pages"):
        _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": [selector]}])


@pytest.mark.parametrize("pages", [["*", "2"], ["1", "1"], ["1-*", "1-*"]])
def test_ambiguous_or_duplicate_query_page_selectors_fail(pages: list[str]) -> None:
    with pytest.raises(PluginConfigError, match="pages"):
        _load(feature_types=["QUERIES"], queries=[{"text": "Total?", "pages": pages}])


@pytest.mark.parametrize("query_field", ["text", "alias"])
def test_query_text_and_alias_reject_provider_disallowed_characters(query_field: str) -> None:
    query: dict[str, object] = {"text": "Total?", query_field: "disallowed\u0000value"}
    with pytest.raises(PluginConfigError, match=query_field):
        _load(feature_types=["QUERIES"], queries=[query])


def test_more_than_thirty_queries_fails() -> None:
    queries = [{"text": f"Question {index}?"} for index in range(31)]
    with pytest.raises(PluginConfigError, match="queries"):
        _load(feature_types=["QUERIES"], queries=queries)


@pytest.mark.parametrize(
    "field",
    [
        "poll_interval_seconds",
        "poll_timeout_seconds",
        "batch_wait_timeout_seconds",
        "max_result_pages",
        "max_blocks",
        "max_result_bytes",
    ],
)
def test_positive_bounds_reject_zero(field: str) -> None:
    with pytest.raises(PluginConfigError, match=field):
        _load(**{field: 0})


def test_poll_backoff_multiplier_must_be_at_least_one() -> None:
    with pytest.raises(PluginConfigError, match="poll_backoff_multiplier"):
        _load(poll_backoff_multiplier=0.5)


def test_poll_max_interval_must_cover_initial_interval() -> None:
    with pytest.raises(PluginConfigError, match="poll_max_interval_seconds"):
        _load(poll_interval_seconds=2.0, poll_max_interval_seconds=1.0)


def test_full_configuration_declares_inputs_and_ordered_outputs() -> None:
    cfg = _load(
        auth_mode="secret_refs",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        version_field="document_version",
        feature_types=["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"],
        queries=[{"text": "What is the total?", "alias": "invoice_total", "pages": ["1-3", "5-*"]}],
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

    assert cfg.declared_input_fields == frozenset({"document_bucket", "document_key", "document_version"})
    assert cfg.configured_output_fields() == {
        "pages": "textract_pages",
        "tables": "textract_tables",
        "forms": "textract_forms",
        "queries": "textract_queries",
        "signatures": "textract_signatures",
        "layout": "textract_layout",
    }
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


class FakeTextractClient:
    def __init__(
        self,
        *,
        pages: list[AnalysisResultPage | Exception],
        start: StartAnalysisReceipt | Exception | None = None,
    ) -> None:
        self.pages = list(pages)
        self.start_result = StartAnalysisReceipt(job_id="job-1") if start is None else start
        self.start_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def start_document_analysis(self, **kwargs: object) -> StartAnalysisReceipt:
        self.start_calls.append(kwargs)
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def get_document_analysis(self, **kwargs: object) -> AnalysisResultPage:
        self.get_calls.append(kwargs)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _page(
    *,
    status: str = "SUCCEEDED",
    blocks: list[dict[str, object]] | None = None,
    next_token: str | None = None,
    page_count: int = 1,
) -> AnalysisResultPage:
    semantic: dict[str, object] = {
        "JobStatus": status,
        "DocumentMetadata": {"Pages": page_count},
        "AnalyzeDocumentModelVersion": "1.0",
        "Blocks": blocks if blocks is not None else [],
    }
    return AnalysisResultPage(semantic_response=semantic, next_token=next_token)


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


def _row(**overrides: object) -> PipelineRow:
    data: dict[str, object] = {"document_bucket": "docs", "document_key": "invoice.pdf"}
    data.update(overrides)
    return make_pipeline_row(data)


def _transform_for_client(client: FakeTextractClient, **overrides: object) -> AWSTextractDocumentAnalysis:
    transform = AWSTextractDocumentAnalysis(_config(**overrides))
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    transform._poll_interval_seconds = 0.001
    transform._poll_max_interval_seconds = 0.001
    transform._row_clients["state-1"] = client
    return transform


def _run(transform: AWSTextractDocumentAnalysis, row: PipelineRow | None = None):
    return transform._process_single_with_state(
        _row() if row is None else row,
        "state-1",
        token_id="token-1",
    )


def test_transform_metadata_and_declared_fields() -> None:
    transform = AWSTextractDocumentAnalysis(_config(page_count_field="textract_pages", extract={"tables": "textract_tables"}))

    assert transform.name == "aws_textract_document_analysis"
    assert transform.determinism is Determinism.EXTERNAL_CALL
    assert transform.passes_through_input is True
    assert transform.creates_tokens is False
    assert transform.audit_characteristics == frozenset({AuditCharacteristic.CREDENTIALS})
    assert transform.declared_input_fields == frozenset({"document_bucket", "document_key"})
    assert transform.declared_output_fields == frozenset({"textract_text", "textract_pages", "textract_tables"})
    assert transform._feature_types == ("FORMS", "TABLES")
    assert transform._sdk_client is None


def test_probe_config_instantiates_without_network() -> None:
    AWSTextractDocumentAnalysis(AWSTextractDocumentAnalysis.probe_config())


def test_process_raises_use_accept() -> None:
    transform = AWSTextractDocumentAnalysis(_config())
    with pytest.raises(NotImplementedError, match="accept"):
        transform.process(_row(), object())  # type: ignore[arg-type]


def test_request_token_is_stable_and_request_sensitive() -> None:
    transform = AWSTextractDocumentAnalysis(_config())
    base = transform._client_request_token(
        run_id="run-1",
        node_id="node-1",
        token_id="token-1",
        bucket="docs",
        key="invoice.pdf",
        version="v1",
    )

    assert len(base) == 64
    assert base == transform._client_request_token(
        run_id="run-1",
        node_id="node-1",
        token_id="token-1",
        bucket="docs",
        key="invoice.pdf",
        version="v1",
    )
    changed = AWSTextractDocumentAnalysis(_config(feature_types=["LAYOUT"]))._client_request_token(
        run_id="run-1",
        node_id="node-1",
        token_id="token-1",
        bucket="docs",
        key="invoice.pdf",
        version="v1",
    )
    assert changed != base


@pytest.mark.parametrize(
    ("row", "reason", "field"),
    [
        (make_pipeline_row({"document_key": "invoice.pdf"}), "missing_field", "document_bucket"),
        (make_pipeline_row({"document_bucket": "docs"}), "missing_field", "document_key"),
        (_row(document_bucket=7), "invalid_input", "document_bucket"),
        (_row(document_key=7), "invalid_input", "document_key"),
        (_row(document_bucket="ab"), "invalid_input", "document_bucket"),
        (_row(document_bucket="bad/bucket"), "invalid_input", "document_bucket"),
        (_row(document_key="   "), "invalid_input", "document_key"),
        (_row(document_key="bad#key"), "invalid_input", "document_key"),
        (_row(document_key="x" * 1025), "invalid_input", "document_key"),
    ],
)
def test_invalid_row_inputs_fail_before_submission(row: PipelineRow, reason: str, field: str) -> None:
    client = FakeTextractClient(pages=[])
    result = _run(_transform_for_client(client), row)

    assert result.status == "error"
    assert result.reason["reason"] == reason
    assert result.reason["field"] == field
    assert client.start_calls == []


@pytest.mark.parametrize(
    ("version", "reason"),
    [(None, "missing_field"), (7, "invalid_input"), (" ", "invalid_input"), ("x" * 1025, "invalid_input")],
)
def test_invalid_configured_version_input_fails(version: object, reason: str) -> None:
    data = {"document_bucket": "docs", "document_key": "invoice.pdf"}
    if version is not None:
        data["document_version"] = version
    client = FakeTextractClient(pages=[])
    result = _run(
        _transform_for_client(client, version_field="document_version"),
        make_pipeline_row(data),
    )

    assert result.status == "error"
    assert result.reason["reason"] == reason
    assert result.reason["field"] == "document_version"
    assert client.start_calls == []


def test_in_progress_then_succeeded_enriches() -> None:
    client = FakeTextractClient(
        pages=[
            _page(status="IN_PROGRESS"),
            _page(blocks=_basic_blocks(text="Complete document")),
        ]
    )
    transform = _transform_for_client(client)

    result = _run(transform)

    assert result.status == "success"
    assert result.row.to_dict() == {
        "document_bucket": "docs",
        "document_key": "invoice.pdf",
        "textract_text": "Complete document",
    }
    assert result.success_reason["action"] == "enriched"
    assert len(client.get_calls) == 2


def _all_facet_blocks() -> list[dict[str, object]]:
    return [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
        {"BlockType": "LINE", "Id": "line-1", "Page": 1, "Text": "Invoice", "Confidence": 99.0},
        {
            "BlockType": "TABLE",
            "Id": "table-1",
            "Page": 1,
            "Confidence": 98.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["cell-1"]}],
        },
        {
            "BlockType": "CELL",
            "Id": "cell-1",
            "Page": 1,
            "RowIndex": 1,
            "ColumnIndex": 1,
            "RowSpan": 1,
            "ColumnSpan": 1,
            "Confidence": 98.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["cell-word"]}],
        },
        {"BlockType": "WORD", "Id": "cell-word", "Page": 1, "Text": "Total", "Confidence": 98.0},
        {
            "BlockType": "KEY_VALUE_SET",
            "Id": "key-1",
            "Page": 1,
            "EntityTypes": ["KEY"],
            "Confidence": 97.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["key-word"]}],
        },
        {"BlockType": "WORD", "Id": "key-word", "Page": 1, "Text": "Reference", "Confidence": 97.0},
        {
            "BlockType": "QUERY",
            "Id": "query-1",
            "Page": 1,
            "Query": {"Text": "What is the total?", "Alias": "total"},
            "Relationships": [{"Type": "ANSWER", "Ids": ["answer-1"]}],
        },
        {"BlockType": "QUERY_RESULT", "Id": "answer-1", "Page": 1, "Text": "$42", "Confidence": 96.0},
        {"BlockType": "SIGNATURE", "Id": "signature-1", "Page": 1, "Confidence": 95.0},
        {
            "BlockType": "LAYOUT_TITLE",
            "Id": "layout-1",
            "Page": 1,
            "Confidence": 94.0,
            "Relationships": [{"Type": "CHILD", "Ids": ["layout-word"]}],
        },
        {"BlockType": "WORD", "Id": "layout-word", "Page": 1, "Text": "Invoice title", "Confidence": 94.0},
    ]


def test_all_configured_projections_are_emitted_only_after_complete_pagination() -> None:
    first = _page(blocks=_basic_blocks(page=1, text="Page one"), next_token="page-2", page_count=2)
    second = _page(blocks=_basic_blocks(page=2, text="Page two"), page_count=2)
    client = FakeTextractClient(pages=[first, second])
    transform = _transform_for_client(
        client,
        feature_types=["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"],
        queries=[{"text": "What is the total?", "alias": "total"}],
        page_count_field="textract_page_count",
        metadata_field="textract_metadata",
        result_field="textract_result",
        extract={"pages": "textract_pages"},
    )

    result = _run(transform)

    assert result.status == "success"
    output = result.row.to_dict()
    assert output["textract_text"] == "Page one\n\f\nPage two"
    assert output["textract_page_count"] == 2
    assert output["textract_metadata"]["block_count"] == 4
    assert len(output["textract_pages"]) == 2
    assert output["textract_result"]["DocumentMetadata"] == {"Pages": 2}
    assert client.get_calls == [
        {"job_id": "job-1", "next_token": None},
        {"job_id": "job-1", "next_token": "page-2"},
    ]


def test_all_normalized_facet_outputs_are_projected() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_all_facet_blocks())])
    transform = _transform_for_client(
        client,
        feature_types=["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"],
        queries=[{"text": "What is the total?", "alias": "total"}],
        extract={
            "pages": "pages",
            "tables": "tables",
            "forms": "forms",
            "queries": "queries",
            "signatures": "signatures",
            "layout": "layout",
        },
    )

    result = _run(transform)

    assert result.status == "success"
    output = result.row.to_dict()
    assert output["pages"][0]["text"] == "Invoice"
    assert output["tables"][0]["rows"][0][0]["text"] == "Total"
    assert output["forms"][0]["key"] == "Reference"
    assert output["queries"][0]["answer"] == "$42"
    assert output["signatures"][0]["id"] == "signature-1"
    assert output["layout"][0]["block_type"] == "LAYOUT_TITLE"


@pytest.mark.parametrize(
    ("status", "reason"),
    [("FAILED", "analysis_failed"), ("PARTIAL_SUCCESS", "partial_success"), ("FUTURE_STATUS", "malformed_response")],
)
def test_terminal_and_unknown_statuses_fail_closed(status: str, reason: str) -> None:
    result = _run(_transform_for_client(FakeTextractClient(pages=[_page(status=status)])))

    assert result.status == "error"
    assert result.reason["reason"] == reason


def test_poll_timeout_is_retryable() -> None:
    transform = _transform_for_client(FakeTextractClient(pages=[_page(status="IN_PROGRESS")]))
    transform._poll_timeout_seconds = 0.0

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "poll_timeout"
    assert result.retryable is True


def test_repeated_pagination_token_fails_closed() -> None:
    client = FakeTextractClient(
        pages=[
            _page(blocks=_basic_blocks(), next_token="repeat"),
            _page(blocks=[], next_token="repeat"),
        ]
    )

    result = _run(_transform_for_client(client))

    assert result.status == "error"
    assert result.reason["reason"] == "pagination_cycle"


def test_result_page_limit_fails_before_extra_request() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks(), next_token="page-2")])

    result = _run(_transform_for_client(client, max_result_pages=1))

    assert result.status == "error"
    assert result.reason["reason"] == "pagination_limit_exceeded"
    assert len(client.get_calls) == 1


def test_frozen_sdk_blocks_enforce_incremental_block_limit_before_pagination() -> None:
    page = AnalysisResultPage(
        semantic_response={
            "JobStatus": "SUCCEEDED",
            "DocumentMetadata": {"Pages": 2},
            "AnalyzeDocumentModelVersion": "1.0",
            "Blocks": tuple(_basic_blocks()),
        },
        next_token="page-2",
    )
    client = FakeTextractClient(pages=[page])

    result = _run(_transform_for_client(client, max_blocks=1))

    assert result.status == "error"
    assert result.reason["reason"] == "too_many_blocks"
    assert len(client.get_calls) == 1


class CancelOnWait:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        return True

    def set(self) -> None:
        return None


def test_shutdown_during_poll_backoff_releases_no_output() -> None:
    transform = _transform_for_client(FakeTextractClient(pages=[_page(status="IN_PROGRESS")]))
    transform._shutdown = CancelOnWait()  # type: ignore[assignment]

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "shutdown_requested"


@pytest.mark.parametrize("retryable", [False, True])
def test_submit_service_error_preserves_retryability(retryable: bool) -> None:
    error = TextractServiceError(code="ThrottlingException" if retryable else "AccessDeniedException", retryable=retryable)
    result = _run(_transform_for_client(FakeTextractClient(pages=[], start=error)))

    assert result.status == "error"
    assert result.reason["reason"] == "submit_failed"
    assert result.retryable is retryable


@pytest.mark.parametrize("retryable", [False, True])
def test_poll_service_error_preserves_retryability(retryable: bool) -> None:
    error = TextractServiceError(code="ThrottlingException" if retryable else "AccessDeniedException", retryable=retryable)
    result = _run(_transform_for_client(FakeTextractClient(pages=[error])))

    assert result.status == "error"
    assert result.reason["reason"] == "poll_failed"
    assert result.retryable is retryable


@pytest.mark.parametrize("phase", ["start", "get"])
def test_idempotency_mismatch_is_a_framework_error(phase: str) -> None:
    client = (
        FakeTextractClient(pages=[], start=TextractIdempotencyInvariantError())
        if phase == "start"
        else FakeTextractClient(pages=[TextractIdempotencyInvariantError()])
    )

    with pytest.raises(FrameworkBugError, match="idempotency"):
        _run(_transform_for_client(client))


def test_malformed_final_result_emits_no_partial_row() -> None:
    client = FakeTextractClient(
        pages=[
            _page(
                blocks=[
                    {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
                    {"BlockType": "LINE", "Id": "line-1", "Page": 1, "Text": "private partial text"},
                ]
            )
        ]
    )
    row = _row(existing="preserve")

    result = _run(_transform_for_client(client), row)

    assert result.status == "error"
    assert result.reason["reason"] == "malformed_response"
    assert result.row is None
    assert row.to_dict() == {"document_bucket": "docs", "document_key": "invoice.pdf", "existing": "preserve"}


def test_client_response_error_maps_to_malformed_response() -> None:
    result = _run(_transform_for_client(FakeTextractClient(pages=[TextractResponseError()])))

    assert result.status == "error"
    assert result.reason["reason"] == "malformed_response"


def test_on_start_requires_landscape() -> None:
    transform = AWSTextractDocumentAnalysis(_config())
    ctx = SimpleNamespace(
        landscape=None,
        node_id="node-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        rate_limit_registry=None,
        shutdown_event=None,
    )

    with pytest.raises(FrameworkBugError, match="Landscape"):
        transform.on_start(ctx)


def test_on_start_builds_with_resolved_secrets_and_close_closes_sdk_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = FakeTextractClient(pages=[])
    sdk.closed = False

    def close() -> None:
        sdk.closed = True

    sdk.close = close  # type: ignore[attr-defined,method-assign]
    captured: dict[str, object] = {}

    def build(**kwargs: object) -> object:
        captured.update(kwargs)
        return sdk

    limiter = object()
    registry = SimpleNamespace(get_limiter=lambda name: limiter if name == "aws_textract_document_analysis" else None)
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_textract_sdk_client",
        build,
    )
    transform = AWSTextractDocumentAnalysis(
        _config(
            auth_mode="secret_refs",
            aws_access_key_id="resolved-access-id",
            aws_secret_access_key="resolved-secret-key",
            aws_session_token="resolved-session-token",
        )
    )
    ctx = SimpleNamespace(
        landscape=object(),
        node_id="node-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        rate_limit_registry=registry,
        shutdown_event=None,
    )

    transform.on_start(ctx)

    assert captured == {
        "region": "ap-southeast-2",
        "aws_access_key_id": "resolved-access-id",
        "aws_secret_access_key": "resolved-secret-key",
        "aws_session_token": "resolved-session-token",
    }
    assert transform._limiter is limiter
    transform.close()
    transform.close()
    assert sdk.closed is True


def test_assistance_distinguishes_async_s3_and_secret_refs_from_inline_plugin() -> None:
    assistance = AWSTextractDocumentAnalysis.get_agent_assistance()

    assert assistance is not None
    rendered = repr(assistance)
    assert "S3" in rendered
    assert "asynchronously" in rendered
    assert "secret_ref" in rendered
    assert "inline" in rendered
