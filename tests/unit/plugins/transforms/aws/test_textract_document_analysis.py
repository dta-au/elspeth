"""Unit tests for the Amazon Textract document-analysis transform."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from elspeth.contracts import AuditCharacteristic, Determinism
from elspeth.contracts.aws_textract import TextractProfiledAuditIdentity, textract_profiled_binding_fingerprint
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws.textract_bucket_region import (
    BucketRegionProof,
    BucketRegionUnverifiedError,
    BucketRegionVerification,
)
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
    assert AWSTextractDocumentAnalysis.web_config_authority is WebConfigAuthority.OPERATOR_PROFILED


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


def test_textract_accepts_exact_supported_region_vocabulary() -> None:
    supported = {
        "ap-northeast-2",
        "ap-south-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ca-central-1",
        "eu-central-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    }

    assert {_load(region=region).region for region in supported} == supported


@pytest.mark.parametrize("region", ["af-south-1", "ap-northeast-1", "cn-north-1", "us-gov-west-1", "moon-east-1"])
def test_textract_rejects_regex_valid_unsupported_region(region: str) -> None:
    with pytest.raises(PluginConfigError, match="supported Amazon Textract region"):
        _load(region=region)


def test_textract_probe_uses_named_supported_non_default_region() -> None:
    probe = AWSTextractDocumentAnalysis.probe_config()

    assert probe["region"] == "ap-southeast-2"
    assert _load(region=probe["region"]).region == probe["region"]


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


def test_batch_wait_reserves_head_bucket_and_textract_sdk_allowances() -> None:
    transform = AWSTextractDocumentAnalysis(
        _config(
            poll_timeout_seconds=5.0,
            batch_wait_timeout_seconds=1.0,
        )
    )

    # HeadBucket: 3 * (10s connect + 30s read). Textract retains its existing
    # 90s SDK overrun after that preflight completes.
    assert transform._effective_batch_wait_timeout_seconds == 215.0


@pytest.mark.parametrize(
    "field",
    [
        "poll_interval_seconds",
        "poll_backoff_multiplier",
        "poll_max_interval_seconds",
        "poll_timeout_seconds",
        "batch_wait_timeout_seconds",
    ],
)
def test_non_finite_timing_configuration_fails(field: str) -> None:
    with pytest.raises(PluginConfigError, match=field):
        _load(**{field: float("inf")})


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


class FakeBucketRegionCoordinator:
    def __init__(self, outcome: BucketRegionVerification | Exception | None = None) -> None:
        self.outcome = outcome or BucketRegionVerification(
            region="ap-southeast-2",
            source="response_header",
            http_status=200,
            cache_status="live",
        )
        self.calls: list[str] = []

    def verify(self, bucket: str, live_verify: object) -> BucketRegionVerification:
        del live_verify
        self.calls.append(bucket)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


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
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator()  # type: ignore[assignment]
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


def test_close_discards_bucket_region_cache_before_restart() -> None:
    transform = AWSTextractDocumentAnalysis(_config())
    original = transform._bucket_region_coordinator
    proof = BucketRegionProof(
        region="ap-southeast-2",
        source="response_header",
        http_status=200,
    )
    assert original.verify("docs", lambda: proof).cache_status == "live"

    transform.close()

    refreshed = transform._bucket_region_coordinator
    assert refreshed is not original
    live_calls: list[str] = []
    verification = refreshed.verify("docs", lambda: live_calls.append("called") or proof)
    assert verification.cache_status == "live"
    assert live_calls == ["called"]


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
    assert result.success_reason["metadata"]["bucket_region_verification"] == {
        "configured_region": "ap-southeast-2",
        "observed_region": "ap-southeast-2",
        "proof_source": "response_header",
        "http_status": 200,
        "cache_status": "live",
    }
    assert len(client.get_calls) == 2


def test_bucket_region_mismatch_stops_before_request_token_or_textract() -> None:
    client = FakeTextractClient(pages=[])
    transform = _transform_for_client(client)
    coordinator = FakeBucketRegionCoordinator(
        BucketRegionVerification(
            region="us-east-1",
            source="error_header",
            http_status=403,
            cache_status="cached",
            provider_code="PermanentRedirect",
        )
    )
    transform._bucket_region_coordinator = coordinator  # type: ignore[assignment]
    transform._client_request_token = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("token computed too early"))  # type: ignore[method-assign]

    result = _run(transform)

    assert result.status == "error"
    assert result.reason == {
        "reason": "bucket_region_mismatch",
        "configured_region": "ap-southeast-2",
        "observed_region": "us-east-1",
        "bucket_region_verification": {
            "configured_region": "ap-southeast-2",
            "observed_region": "us-east-1",
            "proof_source": "error_header",
            "http_status": 403,
            "cache_status": "cached",
            "provider_code": "PermanentRedirect",
        },
    }
    assert result.retryable is False
    assert client.start_calls == []


def test_cached_bucket_region_verification_survives_submit_error() -> None:
    client = FakeTextractClient(
        pages=[],
        start=TextractServiceError(code="ThrottlingException", retryable=True),
    )
    transform = _transform_for_client(client)
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator(  # type: ignore[assignment]
        BucketRegionVerification(
            region="ap-southeast-2",
            source="response_header",
            http_status=200,
            cache_status="cached",
        )
    )

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "submit_failed"
    assert result.reason["bucket_region_verification"] == {
        "configured_region": "ap-southeast-2",
        "observed_region": "ap-southeast-2",
        "proof_source": "response_header",
        "http_status": 200,
        "cache_status": "cached",
    }


def test_cached_bucket_region_verification_survives_poll_error_with_safe_provider_code() -> None:
    client = FakeTextractClient(
        pages=[TextractServiceError(code="AccessDeniedException", retryable=False)],
    )
    transform = _transform_for_client(client)
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator(  # type: ignore[assignment]
        BucketRegionVerification(
            region="ap-southeast-2",
            source="error_header",
            http_status=403,
            cache_status="cached",
            provider_code="AccessDenied",
        )
    )

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "poll_failed"
    evidence = result.reason["bucket_region_verification"]
    assert evidence == {
        "configured_region": "ap-southeast-2",
        "observed_region": "ap-southeast-2",
        "proof_source": "error_header",
        "http_status": 403,
        "cache_status": "cached",
        "provider_code": "AccessDenied",
    }
    assert len(evidence["provider_code"]) <= 128
    assert "\n" not in evidence["provider_code"]


def test_semantic_403_region_proof_is_safe_success_metadata() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _transform_for_client(client)
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator(  # type: ignore[assignment]
        BucketRegionVerification(
            region="ap-southeast-2",
            source="error_header",
            http_status=403,
            cache_status="live",
            provider_code="AccessDenied",
        )
    )

    result = _run(transform)

    assert result.status == "success"
    assert result.success_reason["metadata"]["bucket_region_verification"] == {
        "configured_region": "ap-southeast-2",
        "observed_region": "ap-southeast-2",
        "proof_source": "error_header",
        "http_status": 403,
        "cache_status": "live",
        "provider_code": "AccessDenied",
    }


@pytest.mark.parametrize("retryable", [False, True])
def test_unverified_bucket_region_stops_textract_and_preserves_retryability(retryable: bool) -> None:
    client = FakeTextractClient(pages=[])
    transform = _transform_for_client(client)
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator(  # type: ignore[assignment]
        BucketRegionUnverifiedError(code="transport_error" if retryable else "NoSuchBucket", retryable=retryable)
    )
    transform._client_request_token = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("token computed too early"))  # type: ignore[method-assign]

    result = _run(transform)

    assert result.status == "error"
    assert result.reason == {
        "reason": "bucket_region_unverified",
        "error_type": "transport_error" if retryable else "NoSuchBucket",
    }
    assert result.retryable is retryable
    assert client.start_calls == []


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


def test_successful_get_returning_after_total_deadline_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _transform_for_client(client, poll_timeout_seconds=1.0)
    clock = iter((0.0, 0.25, 1.25))
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.time.monotonic",
        lambda: next(clock),
    )

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "poll_timeout"
    assert result.retryable is True


def test_shutdown_requested_during_successful_get_is_rejected() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _transform_for_client(client)
    shutdown = threading.Event()
    transform._shutdown = shutdown
    original_get = client.get_document_analysis

    def get_and_cancel(**kwargs: object) -> AnalysisResultPage:
        page = original_get(**kwargs)
        shutdown.set()
        return page

    client.get_document_analysis = get_and_cancel  # type: ignore[method-assign]

    result = _run(transform)

    assert result.status == "error"
    assert result.reason["reason"] == "shutdown_requested"


def test_cumulative_result_bytes_stop_pagination_before_another_request() -> None:
    client = FakeTextractClient(
        pages=[
            _page(blocks=_basic_blocks(page=1, text="a" * 600), next_token="page-2", page_count=2),
            _page(blocks=_basic_blocks(page=2, text="b" * 600), next_token="page-3", page_count=2),
            _page(blocks=[]),
        ]
    )

    result = _run(_transform_for_client(client, max_result_bytes=1_000))

    assert result.status == "error"
    assert result.reason["reason"] == "result_too_large"
    assert len(client.get_calls) == 2


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


def test_submit_invalid_s3_object_classifies_access_scope_hint() -> None:
    error = TextractServiceError(code="InvalidS3ObjectException", retryable=False)
    result = _run(_transform_for_client(FakeTextractClient(pages=[], start=error)))

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "submit_failed"
    assert result.reason["error_type"] == "service_error"
    assert result.reason["code"] == "InvalidS3ObjectException"
    assert result.reason["cause"] == "s3_object_unreadable"
    hint = result.reason["error"]
    assert "s3:GetObject" in hint
    assert "s3:GetObjectVersion" in hint
    assert "read scope" in hint
    assert result.retryable is False


def test_submit_other_service_errors_carry_no_s3_scope_hint() -> None:
    error = TextractServiceError(code="AccessDeniedException", retryable=False)
    result = _run(_transform_for_client(FakeTextractClient(pages=[], start=error)))

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "submit_failed"
    assert result.reason["code"] == "AccessDeniedException"
    assert "cause" not in result.reason
    assert "error" not in result.reason


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
    textract_sdk = FakeTextractClient(pages=[])
    textract_sdk.close_count = 0
    s3_sdk = SimpleNamespace(close_count=0)

    def close_textract() -> None:
        textract_sdk.close_count += 1

    def close_s3() -> None:
        s3_sdk.close_count += 1

    textract_sdk.close = close_textract  # type: ignore[attr-defined,method-assign]
    s3_sdk.close = close_s3
    captured: dict[str, dict[str, object]] = {}

    def build_textract(**kwargs: object) -> object:
        captured["textract"] = dict(kwargs)
        return textract_sdk

    def build_s3(**kwargs: object) -> object:
        captured["s3"] = dict(kwargs)
        return s3_sdk

    limiter = object()
    registry = SimpleNamespace(get_limiter=lambda name: limiter if name == "aws_textract_document_analysis" else None)
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_textract_sdk_client",
        build_textract,
    )
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_s3_head_bucket_sdk_client",
        build_s3,
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

    expected = {
        "region": "ap-southeast-2",
        "aws_access_key_id": "resolved-access-id",
        "aws_secret_access_key": "resolved-secret-key",
        "aws_session_token": "resolved-session-token",
    }
    assert captured == {"s3": expected, "textract": expected}
    assert transform._limiter is limiter
    transform.close()
    transform.close()
    assert textract_sdk.close_count == 1
    assert s3_sdk.close_count == 1


def test_on_start_closes_s3_when_textract_client_construction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    s3_sdk = SimpleNamespace(close_count=0)

    def close_s3() -> None:
        s3_sdk.close_count += 1

    s3_sdk.close = close_s3
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_s3_head_bucket_sdk_client",
        lambda **_kwargs: s3_sdk,
    )
    monkeypatch.setattr(
        "elspeth.plugins.transforms.aws.textract_document_analysis.build_textract_sdk_client",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("construction failed")),
    )
    transform = AWSTextractDocumentAnalysis(_config())
    ctx = SimpleNamespace(
        landscape=object(),
        node_id="node-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        rate_limit_registry=None,
        shutdown_event=None,
    )

    with pytest.raises(RuntimeError, match="construction failed"):
        transform.on_start(ctx)

    assert s3_sdk.close_count == 1
    assert transform._s3_sdk_client is None
    assert transform._sdk_client is None


def test_assistance_distinguishes_async_s3_and_secret_refs_from_inline_plugin() -> None:
    assistance = AWSTextractDocumentAnalysis.get_agent_assistance()

    assert assistance is not None
    rendered = repr(assistance)
    assert "S3" in rendered
    assert "asynchronously" in rendered
    assert "secret_ref" in rendered
    assert "inline" in rendered


def _bucket_config(**overrides: object) -> dict[str, object]:
    config = _config(bucket="operator-owned-docs", key_prefix="scans/incoming")
    config.pop("bucket_field")
    config.update(overrides)
    return config


def _load_bucket_mode(**overrides: object) -> AWSTextractDocumentAnalysisConfig:
    return AWSTextractDocumentAnalysisConfig.from_dict(
        _bucket_config(**overrides),
        plugin_name="aws_textract_document_analysis",
    )


def test_static_bucket_mode_loads_and_declares_key_only() -> None:
    cfg = _load_bucket_mode()

    assert cfg.bucket == "operator-owned-docs"
    assert cfg.key_prefix == "scans/incoming"
    assert cfg.bucket_field is None
    assert cfg.declared_input_fields == frozenset({"document_key"})


def test_static_bucket_and_bucket_field_are_mutually_exclusive() -> None:
    with pytest.raises(PluginConfigError, match="mutually exclusive"):
        _load(bucket="operator-owned-docs")


# ── Input options must name arriving columns, not created ones ─────────────
#
# key_field / bucket_field / version_field locate the document; the output
# targets are written back onto the same row. Pointing a locator at a target
# makes the transform overwrite the column it reads. Nothing downstream
# catches it: the executor's collision check compares declared_output_fields
# against the input keys OF THE ROW, so it fires only once a row carries the
# column, and under mode: observed there is no declared field for DAG
# validation to carry (elspeth-09dc6407f1).


@pytest.mark.parametrize("option", ["key_field", "bucket_field", "version_field"])
def test_a_document_locator_naming_an_output_target_is_rejected(option: str) -> None:
    with pytest.raises(PluginConfigError, match=f"{option} names 'textract_text', which aws_textract_document_analysis itself creates"):
        AWSTextractDocumentAnalysis(_config(**{option: "textract_text"}))


def test_a_locator_naming_an_extract_facet_target_is_rejected() -> None:
    with pytest.raises(PluginConfigError, match="key_field names 'textract_forms', which aws_textract_document_analysis itself creates"):
        AWSTextractDocumentAnalysis(_config(key_field="textract_forms", extract={"forms": "textract_forms"}))


def test_the_error_names_the_offending_value_and_the_plugin() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        AWSTextractDocumentAnalysis(_config(bucket_field="textract_text"))

    message = str(excinfo.value)
    assert "bucket_field names 'textract_text', which aws_textract_document_analysis itself creates" in message
    assert "Point bucket_field at a column that ARRIVES on the row" in message


def test_every_offending_locator_is_named_at_once() -> None:
    """One pass over the options, so repointing one does not just reveal the next."""
    with pytest.raises(PluginConfigError) as excinfo:
        AWSTextractDocumentAnalysis(_config(key_field="textract_text", bucket_field="textract_text"))

    message = str(excinfo.value)
    assert "bucket_field names 'textract_text'" in message
    assert "key_field names 'textract_text'" in message
    assert "Point bucket_field and key_field at a column that ARRIVES on the row" in message


def test_locators_naming_arriving_columns_still_construct() -> None:
    transform = AWSTextractDocumentAnalysis(_config(version_field="document_version"))

    assert transform.declared_input_fields == frozenset({"document_bucket", "document_key", "document_version"})


def test_one_of_static_bucket_or_bucket_field_is_required() -> None:
    config = _config()
    config.pop("bucket_field")

    with pytest.raises(PluginConfigError, match="bucket or bucket_field"):
        AWSTextractDocumentAnalysisConfig.from_dict(config, plugin_name="aws_textract_document_analysis")


def test_key_prefix_requires_static_bucket() -> None:
    with pytest.raises(PluginConfigError, match="key_prefix requires"):
        _load(key_prefix="scans/incoming")


@pytest.mark.parametrize("bucket", ["ab", "scans/incoming", "bad bucket", "x" * 256])
def test_static_bucket_must_be_a_well_formed_s3_bucket_name(bucket: str) -> None:
    with pytest.raises(PluginConfigError, match="well-formed S3 bucket name"):
        _load_bucket_mode(bucket=bucket)


@pytest.mark.parametrize("prefix", ["/absolute", "trailing/", "a//b", "../up", "a\\b", ".", "s3:scheme"])
def test_key_prefix_must_be_a_canonical_relative_path(prefix: str) -> None:
    with pytest.raises(PluginConfigError, match="key_prefix must"):
        _load_bucket_mode(key_prefix=prefix)


def _bucket_mode_transform_for_client(client: FakeTextractClient, **overrides: object) -> AWSTextractDocumentAnalysis:
    transform = AWSTextractDocumentAnalysis(_bucket_config(**overrides))
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    transform._poll_interval_seconds = 0.001
    transform._poll_max_interval_seconds = 0.001
    transform._row_clients["state-1"] = client
    transform._bucket_region_coordinator = FakeBucketRegionCoordinator()  # type: ignore[assignment]
    return transform


def test_bucket_mode_joins_row_key_under_prefix() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _bucket_mode_transform_for_client(client)

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": "invoice.pdf"}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "success"
    assert client.start_calls[0]["bucket"] == "operator-owned-docs"
    assert client.start_calls[0]["key"] == "scans/incoming/invoice.pdf"


def test_bucket_mode_without_prefix_uses_row_key_directly() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _bucket_mode_transform_for_client(client, key_prefix=None)

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": "invoice.pdf"}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "success"
    assert client.start_calls[0]["bucket"] == "operator-owned-docs"
    assert client.start_calls[0]["key"] == "invoice.pdf"


@pytest.mark.parametrize(
    "row_key",
    [
        "/absolute.pdf",
        "../escape.pdf",
        "a//b.pdf",
        "scans/",
        "s3:scheme.pdf",
        "s3://other-bucket/secret.pdf",
        "C:\\secret.pdf",
        "records\\secret.pdf",
        "records/../secret.pdf",
        "records/./secret.pdf",
        " leading-space.pdf",
        "trailing-space.pdf ",
        "control\x1fchar.pdf",
        "delete\x7fchar.pdf",
        "",
        ".",
        "..",
    ],
)
def test_bucket_mode_rejects_non_relative_row_keys(row_key: str) -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client)

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": row_key}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["field"] == "document_key"
    assert client.start_calls == []


def test_bucket_mode_rejects_overlong_joined_key() -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client, key_prefix="p" * 1000)

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": "k" * 30}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["field"] == "document_key"
    assert client.start_calls == []


def test_bucket_mode_does_not_require_a_bucket_column_and_missing_key_fails() -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client)

    result = transform._process_single_with_state(
        make_pipeline_row({"unrelated": "value"}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "missing_field"
    assert result.reason["field"] == "document_key"


def _profiled_identity(**overrides: object) -> TextractProfiledAuditIdentity:
    binding: dict[str, object] = {"bucket": "operator-owned-docs", "region": "ap-southeast-2", "key_prefix": "scans/incoming"}
    binding.update(overrides)
    return TextractProfiledAuditIdentity(
        profile_alias="acceptance-docs",
        binding_fingerprint=textract_profiled_binding_fingerprint(
            bucket=str(binding["bucket"]),
            region=str(binding["region"]),
            key_prefix=None if binding["key_prefix"] is None else str(binding["key_prefix"]),
        ),
    )


def _profiled_safe_config() -> dict[str, object]:
    return {
        "profile": "acceptance-docs",
        "key_field": "document_key",
        "feature_types": ["FORMS", "TABLES"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }


def test_profiled_bind_projects_call_record_identity_with_relative_key() -> None:
    client = FakeTextractClient(pages=[_page(blocks=_basic_blocks())])
    transform = _bucket_mode_transform_for_client(client)
    transform._bind_profiled_audit_identity(_profiled_identity(), audit_safe_config=_profiled_safe_config())

    result = transform._process_single_with_state(
        make_pipeline_row({"document_key": "invoice.pdf"}),
        "state-1",
        token_id="token-1",
    )

    assert result.status == "success"
    assert client.start_calls[0]["bucket"] == "operator-owned-docs"
    assert client.start_calls[0]["key"] == "scans/incoming/invoice.pdf"
    assert client.start_calls[0]["audit_identity"] == {"profile": "acceptance-docs", "key": "invoice.pdf"}
    assert transform.config == _profiled_safe_config()


def test_profiled_bind_rejects_binding_fingerprint_mismatch() -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client)

    with pytest.raises(ValueError, match="does not match"):
        transform._bind_profiled_audit_identity(
            _profiled_identity(bucket="a-different-bucket"),
            audit_safe_config=_profiled_safe_config(),
        )


def test_profiled_bind_requires_static_bucket_mode() -> None:
    client = FakeTextractClient(pages=[])
    transform = _transform_for_client(client)

    with pytest.raises(ValueError, match="static bucket"):
        transform._bind_profiled_audit_identity(_profiled_identity(), audit_safe_config=_profiled_safe_config())


def test_profiled_bind_rejects_private_binding_field_in_safe_config() -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client)
    poisoned = {**_profiled_safe_config(), "bucket": "leaked"}

    with pytest.raises(ValueError, match="private binding field"):
        transform._bind_profiled_audit_identity(_profiled_identity(), audit_safe_config=poisoned)


def test_profiled_bind_rejects_nominal_type_impostor() -> None:
    client = FakeTextractClient(pages=[])
    transform = _bucket_mode_transform_for_client(client)

    class _Impostor:
        profile_alias = "acceptance-docs"
        binding_fingerprint = "0" * 64

    with pytest.raises(TypeError, match="TextractProfiledAuditIdentity"):
        transform._bind_profiled_audit_identity(_Impostor(), audit_safe_config=_profiled_safe_config())


def test_preflight_binder_binds_every_profiled_textract_transform_or_refuses() -> None:
    from types import SimpleNamespace

    from elspeth.web.execution.preflight import bind_profiled_textract_audit_identities
    from elspeth.web.plugin_policy.models import PluginId

    transform = AWSTextractDocumentAnalysis(_bucket_config())
    bundle = SimpleNamespace(
        transforms=(
            SimpleNamespace(plugin=transform, settings=SimpleNamespace(plugin="aws_textract_document_analysis", name="textract_1")),
        )
    )
    plugin_id = PluginId("transform", "aws_textract_document_analysis")
    snapshot = SimpleNamespace(usable_profile_aliases=((plugin_id, ("acceptance-docs",)),))
    identity = _profiled_identity()

    bind_profiled_textract_audit_identities(
        bundle,  # type: ignore[arg-type]
        authored_options_by_node={"textract_1": _profiled_safe_config()},
        plugin_snapshot=snapshot,  # type: ignore[arg-type]
        profiled_textract_audit_identities=(("textract_1", identity),),
    )
    assert transform._profiled_audit_identity is identity

    empty_bundle = SimpleNamespace(transforms=())
    with pytest.raises(ValueError, match="do not match the runtime transform set"):
        bind_profiled_textract_audit_identities(
            empty_bundle,  # type: ignore[arg-type]
            authored_options_by_node={},
            plugin_snapshot=snapshot,  # type: ignore[arg-type]
            profiled_textract_audit_identities=(("textract_1", identity),),
        )

    unprofiled_snapshot = SimpleNamespace(usable_profile_aliases=())
    with pytest.raises(ValueError, match="matching frozen plugin profile"):
        bind_profiled_textract_audit_identities(
            empty_bundle,  # type: ignore[arg-type]
            authored_options_by_node={},
            plugin_snapshot=unprofiled_snapshot,  # type: ignore[arg-type]
            profiled_textract_audit_identities=(("textract_1", identity),),
        )
