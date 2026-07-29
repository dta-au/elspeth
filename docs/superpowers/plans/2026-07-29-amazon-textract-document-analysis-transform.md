# Amazon Textract Document Analysis Transform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the audited, asynchronous, S3-backed `aws_textract_document_analysis` enrichment transform defined by the approved 2026-07-29 specification.

**Architecture:** A `BatchTransformMixin` transform owns configuration, row lifecycle, polling, pagination, and schema propagation. A focused `AuditedClientBase` adapter owns boto3 calls, audit records, telemetry, and error classification; a pure parser owns all Tier 3 block validation and normalized projections. Explicit AWS credentials flow through ELSPETH's existing secret-ref resolver, while deployed workloads may use boto3's default chain.

**Tech Stack:** Python 3.13, Pydantic v2, boto3/botocore, ELSPETH plugin contracts, Landscape audit payloads, pytest, Hypothesis, Ruff/mypy through `elspeth-lints`, Wardline.

**Approved specification:** `docs/superpowers/specs/2026-07-29-amazon-textract-document-analysis-transform-design.md`

---

## File structure

### Production files

- Create `src/elspeth/plugins/transforms/aws/textract_document_analysis.py` — Pydantic configuration, plugin metadata, batching lifecycle, idempotency, polling/pagination orchestration, row projection, invariant probe, and assistance text.
- Create `src/elspeth/plugins/transforms/aws/textract_client.py` — boto3 protocol/build function, audited start/get operations, sanitized SDK errors, response metadata extraction, and audit-before-telemetry emission.
- Create `src/elspeth/plugins/transforms/aws/textract_result.py` — pure response aggregation, block graph validation, normalized text/page/table/form/query/signature/layout projections, and native aggregate construction.
- Modify `src/elspeth/core/secrets.py` — recognize `aws_access_key_id` as credential-bearing on every validation/fingerprinting surface.

Dynamic plugin discovery already scans `src/elspeth/plugins/transforms/aws`; no registry import or manager modification is required. Boto3 remains a lazy import so base installations without the `aws` extra can still discover the plugin and report the existing optional-extra guidance at construction/startup.

### Test and generated files

- Create `tests/unit/plugins/transforms/aws/test_textract_document_analysis.py`.
- Create `tests/unit/plugins/transforms/aws/test_textract_client.py`.
- Create `tests/unit/plugins/transforms/aws/test_textract_result.py`.
- Create `tests/property/plugins/transforms/aws/test_textract_result_properties.py`.
- Create `tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py`.
- Create `tests/integration/plugins/transforms/aws/test_textract_document_analysis_pipeline.py`.
- Create `tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py`.
- Modify `tests/unit/core/test_resolve_secret_refs.py`.
- Modify `tests/unit/web/execution/test_validation.py`.
- Modify `tests/unit/web/catalog/test_service.py`.
- Create generated snapshot `tests/golden/web/catalog/knob_schema/transform__aws_textract_document_analysis.json`.

---

### Task 1: Secret policy and configuration contract

**Files:**

- Create: `src/elspeth/plugins/transforms/aws/textract_document_analysis.py`
- Modify: `src/elspeth/core/secrets.py`
- Create: `tests/unit/plugins/transforms/aws/test_textract_document_analysis.py`
- Modify: `tests/unit/core/test_resolve_secret_refs.py`

- [x] **Step 1: Write failing central secret-policy tests**

Add imports for `collect_credential_field_violations`, `collect_disallowed_secret_ref_markers`, and `is_secret_field` to `tests/unit/core/test_resolve_secret_refs.py`, then add:

```python
def test_aws_access_key_id_is_a_credential_field() -> None:
    assert is_secret_field("aws_access_key_id") is True


def test_literal_aws_access_key_id_is_rejected_as_credential() -> None:
    options = {"aws_access_key_id": "literal-access-key-id"}
    assert collect_credential_field_violations(options) == ["aws_access_key_id"]


def test_secret_ref_is_allowed_in_aws_access_key_id() -> None:
    options = {"aws_access_key_id": {"secret_ref": "AWS_ACCESS_KEY_ID"}}
    assert collect_disallowed_secret_ref_markers(options) == []
```

- [x] **Step 2: Run the central tests and verify the new exact-name case fails**

Run:

```bash
pytest tests/unit/core/test_resolve_secret_refs.py -q
```

Expected: the existing tests pass and `test_aws_access_key_id_is_a_credential_field` fails because the exact name is not yet in `SECRET_FIELD_NAMES`.

- [x] **Step 3: Add the exact credential field to the central policy**

Add this member to `SECRET_FIELD_NAMES` in `src/elspeth/core/secrets.py`:

```python
        "aws_access_key_id",
```

Keep `aws_secret_access_key` and `aws_session_token` out of the exact set because the existing `_key` and `_token` suffixes already classify them.

- [x] **Step 4: Run the central secret-policy tests**

Run:

```bash
pytest tests/unit/core/test_resolve_secret_refs.py -q
```

Expected: PASS.

- [x] **Step 5: Write failing configuration-model tests**

Create `tests/unit/plugins/transforms/aws/test_textract_document_analysis.py` with a reusable minimal config and these assertions:

```python
from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.aws.textract_document_analysis import (
    AWSTextractDocumentAnalysisConfig,
)


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


def test_minimal_default_chain_config() -> None:
    cfg = AWSTextractDocumentAnalysisConfig.from_dict(
        _config(), plugin_name="aws_textract_document_analysis"
    )
    assert cfg.auth_mode == "default_chain"
    assert cfg.feature_types == ["FORMS", "TABLES"]
    assert cfg.all_output_field_names() == ["textract_text"]


def test_secret_refs_mode_requires_access_and_secret_key() -> None:
    with pytest.raises(PluginConfigError, match="required together"):
        AWSTextractDocumentAnalysisConfig.from_dict(
            _config(auth_mode="secret_refs", aws_access_key_id="resolved-id"),
            plugin_name="aws_textract_document_analysis",
        )


def test_default_chain_rejects_explicit_credentials() -> None:
    with pytest.raises(PluginConfigError, match="forbidden in default_chain"):
        AWSTextractDocumentAnalysisConfig.from_dict(
            _config(aws_access_key_id="resolved-id", aws_secret_access_key="resolved-secret"),
            plugin_name="aws_textract_document_analysis",
        )


def test_queries_require_queries_feature() -> None:
    with pytest.raises(PluginConfigError, match="QUERIES"):
        AWSTextractDocumentAnalysisConfig.from_dict(
            _config(queries=[{"text": "What is the total?"}]),
            plugin_name="aws_textract_document_analysis",
        )


def test_duplicate_output_names_fail() -> None:
    with pytest.raises(PluginConfigError, match="Duplicate output field"):
        AWSTextractDocumentAnalysisConfig.from_dict(
            _config(text_field="result", metadata_field="result"),
            plugin_name="aws_textract_document_analysis",
        )
```

Add parameterized cases covering all invalid feature names, duplicate features, empty outputs, more than 30 queries, invalid query page selectors, invalid region/bucket/key/version field names, non-positive bounds, `poll_max_interval_seconds < poll_interval_seconds`, and session token without the credential pair. Add a positive full configuration that maps all six `extract` members and checks `declared_input_fields` plus output ordering.

- [x] **Step 6: Run the configuration tests and verify import failure**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_document_analysis.py -q
```

Expected: collection fails because `textract_document_analysis.py` does not exist.

- [x] **Step 7: Implement the frozen config models**

Create `src/elspeth/plugins/transforms/aws/textract_document_analysis.py` with lazy runtime imports and these public models/constants:

```python
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.plugins.infrastructure.config_base import TransformDataConfig

FeatureType = Literal["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"]
AuthMode = Literal["default_chain", "secret_refs"]

_FEATURE_TYPES = frozenset({"TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"})
_REGION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_QUERY_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9\s!\"#$%'&()*+,\-./:;=?@[\\\]^_`{|}~><]+$")
_QUERY_PAGE_PATTERN = re.compile(r"^[0-9*\-]+$")


class TextractQueryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=200)
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    pages: list[str] = Field(default_factory=list)


class TextractExtractFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: str | None = None
    tables: str | None = None
    forms: str | None = None
    queries: str | None = None
    signatures: str | None = None
    layout: str | None = None


class AWSTextractDocumentAnalysisConfig(TransformDataConfig):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        **TransformDataConfig.model_config,
        hide_input_in_errors=True,
    )

    region: str = Field(min_length=1, max_length=64)
    auth_mode: AuthMode = "default_chain"
    aws_access_key_id: str | None = Field(default=None, min_length=1, max_length=256, repr=False)
    aws_secret_access_key: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    aws_session_token: str | None = Field(default=None, min_length=1, max_length=16384, repr=False)

    bucket_field: str = Field(min_length=1, max_length=256)
    key_field: str = Field(min_length=1, max_length=256)
    version_field: str | None = Field(default=None, min_length=1, max_length=256)
    feature_types: list[FeatureType] = Field(min_length=1, max_length=5)
    queries: list[TextractQueryConfig] = Field(default_factory=list, max_length=30)

    text_field: str | None = None
    page_count_field: str | None = None
    metadata_field: str | None = None
    extract: TextractExtractFields = Field(default_factory=TextractExtractFields)
    result_field: str | None = None

    poll_interval_seconds: float = Field(default=1.0, gt=0)
    poll_backoff_multiplier: float = Field(default=1.5, ge=1)
    poll_max_interval_seconds: float = Field(default=10.0, gt=0)
    poll_timeout_seconds: float = Field(default=3600.0, gt=0)
    batch_wait_timeout_seconds: float = Field(default=3900.0, gt=0)
    max_result_pages: int = Field(default=1000, gt=0)
    max_blocks: int = Field(default=200_000, gt=0)
    max_result_bytes: int = Field(default=50_000_000, gt=0)
```

Implement validators that:

- require `_REGION_PATTERN.fullmatch(region)`;
- require every output/input field name to be non-whitespace;
- reject duplicate feature types without reordering the user-visible config;
- validate query text/alias with `_QUERY_TEXT_PATTERN`;
- parse page selectors as `*`, positive integer, `start-end`, or `start-*`, rejecting duplicates, zero, descending ranges, and `*` mixed with other selectors;
- require queries iff `QUERIES` is selected;
- require the resolved access/secret pair in `secret_refs` mode and reject all three credential fields in `default_chain` mode;
- require `poll_max_interval_seconds >= poll_interval_seconds`;
- require at least one output target and reject duplicate output names.

Implement `configured_output_fields()`, `all_output_field_names()`, and:

```python
    @property
    def declared_input_fields(self) -> frozenset[str]:
        fields = {self.bucket_field, self.key_field}
        if self.version_field is not None:
            fields.add(self.version_field)
        return super().declared_input_fields | frozenset(fields)
```

Do not define the plugin class yet; dynamic discovery will ignore a module that contains only config models.

- [x] **Step 8: Run configuration and secret-policy tests**

Run:

```bash
pytest tests/unit/core/test_resolve_secret_refs.py tests/unit/plugins/transforms/aws/test_textract_document_analysis.py -q
```

Expected: PASS.

- [x] **Step 9: Commit Task 1**

```bash
git add src/elspeth/core/secrets.py src/elspeth/plugins/transforms/aws/textract_document_analysis.py tests/unit/core/test_resolve_secret_refs.py tests/unit/plugins/transforms/aws/test_textract_document_analysis.py
git commit -m "feat(textract): define secure plugin configuration"
```

---

### Task 2: Pure block aggregation and normalized projections

**Files:**

- Create: `src/elspeth/plugins/transforms/aws/textract_result.py`
- Create: `tests/unit/plugins/transforms/aws/test_textract_result.py`
- Create: `tests/property/plugins/transforms/aws/test_textract_result_properties.py`

- [x] **Step 1: Write failing text/page/native-result tests**

Create `tests/unit/plugins/transforms/aws/test_textract_result.py` with a two-page synthetic response. Use only provider-shaped dictionaries, including `PAGE` child relationships to `LINE` blocks and line child relationships to `WORD` blocks. Assert:

```python
from __future__ import annotations

import pytest

from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_textract_result,
)


def test_normalize_text_pages_metadata_and_native_result() -> None:
    result = normalize_textract_result(
        job_id="job-1",
        result_pages=[_first_page(), _second_page()],
        feature_types=("FORMS", "TABLES"),
        s3_version="version-1",
        max_blocks=200_000,
        max_result_bytes=50_000_000,
    )
    assert result.text == "Invoice\nTotal $42\n\f\nThank you"
    assert result.page_count == 2
    assert result.block_count == len(result.native_result["Blocks"])
    assert result.metadata["job_id"] == "job-1"
    assert "ResponseMetadata" not in result.native_result
    assert "NextToken" not in result.native_result


def test_duplicate_block_id_fails_closed() -> None:
    page = _first_page()
    page["Blocks"].append(dict(page["Blocks"][0]))
    with pytest.raises(MalformedTextractResponse, match="duplicate block id"):
        normalize_textract_result(
            job_id="job-1",
            result_pages=[page],
            feature_types=("FORMS",),
            s3_version=None,
            max_blocks=100,
            max_result_bytes=100_000,
        )
```

Add focused failing cases for page-count disagreement, missing required keys, non-list blocks, dangling relationships, invalid page/confidence/geometry values, too many blocks, and an oversized aggregate.

- [x] **Step 2: Run parser tests and verify import failure**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_result.py -q
```

Expected: collection fails because `textract_result.py` does not exist.

- [x] **Step 3: Implement aggregation primitives and text/page projection**

Create `src/elspeth/plugins/transforms/aws/textract_result.py` with:

```python
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from elspeth.core.canonical import canonical_json


class MalformedTextractResponse(ValueError):
    def __init__(self, message: str = "malformed Amazon Textract response") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NormalizedTextractResult:
    text: str
    page_count: int
    block_count: int
    pages: tuple[dict[str, Any], ...]
    tables: tuple[dict[str, Any], ...]
    forms: tuple[dict[str, Any], ...]
    queries: tuple[dict[str, Any], ...]
    signatures: tuple[dict[str, Any], ...]
    layout: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    native_result: dict[str, Any]


def normalize_textract_result(
    *,
    job_id: str,
    result_pages: Sequence[Mapping[str, Any]],
    feature_types: tuple[str, ...],
    s3_version: str | None,
    max_blocks: int,
    max_result_bytes: int,
) -> NormalizedTextractResult:
    """Validate a complete SUCCEEDED response sequence and normalize it."""
```

Implement strict helpers `_mapping`, `_sequence`, `_required_str`, `_optional_str`, `_bounded_int`, `_finite_float`, `_geometry`, `_relationships`, `_child_ids`, and `_text_from_children`. Aggregate only `JobStatus`, `DocumentMetadata`, `AnalyzeDocumentModelVersion`, `Warnings`, and `Blocks`; preserve unknown members inside copied blocks and omit unknown top-level response members.

Require every result page to be `SUCCEEDED`, require stable page count/model version, concatenate blocks in API order, reject duplicate IDs, then compare PAGE blocks with `DocumentMetadata.Pages`. Build page text from `LINE` blocks in provider arrival order and join pages with `"\n\f\n"`. Use `canonical_json(native_result).encode("utf-8")` for the final exact size check.

- [x] **Step 4: Run text/page parser tests**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_result.py -q
```

Expected: the text/page/native tests pass; facet tests added next are still absent.

- [x] **Step 5: Add failing table/form/query/signature/layout tests**

Extend the fixture with `TABLE`, `CELL`, `KEY_VALUE_SET`, `QUERY`, `QUERY_RESULT`, `SIGNATURE`, and `LAYOUT_*` blocks. Assert exact normalized dictionaries from the approved specification, including sparse cell coordinates, an unanswered query with null answer fields, and a valid form key without a value. Add failures for duplicate cell coordinates, non-positive spans, query results linked from the wrong block type, and cyclic child relationships.

- [x] **Step 6: Implement all facet projections**

Add `_project_tables`, `_project_forms`, `_project_queries`, `_project_signatures`, and `_project_layout`, each returning `tuple[dict[str, Any], ...]`. Their exact algorithms are:

- `_project_tables`: visit `TABLE` blocks in provider order; follow their `CHILD` IDs to `CELL` and `MERGED_CELL` blocks; validate positive integer `RowIndex`, `ColumnIndex`, `RowSpan`, and `ColumnSpan`; reject duplicate `(row_index, column_index)` anchors within one table; resolve cell text recursively; emit `id`, `page`, `confidence`, `geometry`, `row_count`, `column_count`, and provider-ordered `cells` using the specification's exact key names.
- `_project_forms`: visit `KEY_VALUE_SET` blocks whose `EntityTypes` contains `KEY`; resolve key text through `CHILD`, follow at most one `VALUE` relationship to a `KEY_VALUE_SET` block whose `EntityTypes` contains `VALUE`, and emit `key`, nullable `value`, confidence, geometry, page, key block ID, and nullable value block ID. Reject wrong relationship targets and multiple values.
- `_project_queries`: visit `QUERY` blocks, require `Query.Text`, copy optional `Query.Alias`, follow at most one `ANSWER` relationship to `QUERY_RESULT`, and emit nullable answer text/confidence/geometry/ID when no result exists. Reject wrong targets and multiple answers.
- `_project_signatures`: visit `SIGNATURE` blocks and emit `id`, `page`, `confidence`, and geometry without following relationships.
- `_project_layout`: visit every block whose type starts with `LAYOUT_`; convert the suffix to lowercase `type`, resolve descendant text through `CHILD`, and emit `id`, `page`, `type`, text, confidence, and geometry.

Every function validates each consumed member, retains provider order within a page, and orders page groups numerically. `_text_from_children` carries an active-ID recursion set so relationship cycles raise `MalformedTextractResponse` instead of recursing.

- [x] **Step 7: Add property tests for pagination cuts and mapping order**

Create `tests/property/plugins/transforms/aws/test_textract_result_properties.py`. Generate a fixed semantic block sequence, split it at Hypothesis-generated cut points, independently shuffle dictionary insertion order, and assert `text`, all facet tuples, metadata, and native block order are identical. Do not shuffle the provider block sequence itself because provider arrival order is part of the public contract.

- [x] **Step 8: Run pure parser tests**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_result.py tests/property/plugins/transforms/aws/test_textract_result_properties.py -q
```

Expected: PASS.

- [x] **Step 9: Commit Task 2**

```bash
git add src/elspeth/plugins/transforms/aws/textract_result.py tests/unit/plugins/transforms/aws/test_textract_result.py tests/property/plugins/transforms/aws/test_textract_result_properties.py
git commit -m "feat(textract): normalize document analysis blocks"
```

---

### Task 3: Audited boto3 client

**Files:**

- Create: `src/elspeth/plugins/transforms/aws/textract_client.py`
- Create: `tests/unit/plugins/transforms/aws/test_textract_client.py`

- [x] **Step 1: Write failing audited-client tests**

Create fakes for a call recorder, telemetry callback, limiter, and SDK protocol. The recorder must implement `allocate_call_index()` and `record_call()` and append `"audit"` to an order list; telemetry appends `"telemetry"`.

Add tests that assert:

```python
def test_start_records_before_telemetry_and_returns_job_id() -> None:
    sdk = FakeSDK(start_response={"JobId": "job-1", "ResponseMetadata": _metadata()})
    client, recorder, order = _client(sdk)
    receipt = client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version="v1",
        feature_types=("FORMS", "TABLES"),
        queries=(),
        client_request_token="a" * 64,
    )
    assert receipt.job_id == "job-1"
    assert sdk.start_requests == [{
        "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf", "Version": "v1"}},
        "FeatureTypes": ["FORMS", "TABLES"],
        "ClientRequestToken": "a" * 64,
    }]
    assert order == ["audit", "telemetry"]
    assert recorder.calls[0]["request_data"].to_dict()["client_request_token_fingerprint"]
    assert "a" * 64 not in repr(recorder.calls[0])


def test_get_replaces_raw_next_token_with_fingerprint_in_audit() -> None:
    sdk = FakeSDK(get_responses=[{
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [],
        "NextToken": "opaque-next-token",
        "ResponseMetadata": _metadata(),
    }])
    client, recorder, _ = _client(sdk)
    page = client.get_document_analysis(job_id="job-1", next_token=None)
    assert page.next_token == "opaque-next-token"
    audited = recorder.calls[0]["response_data"].to_dict()
    assert "opaque-next-token" not in repr(audited)
    assert audited["next_token_present"] is True
```

Add tests for no-version/no-query request omission, `QueriesConfig` shape, limiter acquisition, attempt extraction, malformed response, oversized semantic response, all retryable and non-retryable botocore codes, raw-message redaction, idempotency mismatch's distinct exception, and telemetry not firing when audit persistence fails.

- [x] **Step 2: Run client tests and verify import failure**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_client.py -q
```

Expected: collection fails because `textract_client.py` does not exist.

- [x] **Step 3: Implement SDK protocol, receipts, and sanitized errors**

Create `src/elspeth/plugins/transforms/aws/textract_client.py` with:

```python
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase, TelemetryEmitCallback


class TextractResponseError(ValueError):
    pass


class TextractServiceError(RuntimeError):
    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__("Amazon Textract request failed")
        self.code = code
        self.retryable = retryable


class TextractIdempotencyInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StartAnalysisReceipt:
    job_id: str


@dataclass(frozen=True, slots=True)
class AnalysisResultPage:
    semantic_response: Mapping[str, Any]
    next_token: str | None


class TextractSDKClient(Protocol):
    def start_document_analysis(self, **kwargs: Any) -> Mapping[str, Any]:
        raise NotImplementedError

    def get_document_analysis(self, **kwargs: Any) -> Mapping[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
```

Define `_RETRYABLE_CODES` as exactly `InternalServerError`, `ThrottlingException`, `ProvisionedThroughputExceededException`, and `LimitExceededException`. Treat botocore connection/endpoint/read-timeout exceptions as retryable with code `transport_error`. Bound every AWS code to 128 characters and never retain `Error.Message`.

- [x] **Step 4: Implement the client builder and audited operations**

Implement:

```python
def build_textract_sdk_client(
    *,
    region: str,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
) -> TextractSDKClient:
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": region,
        "config": Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    }
    if aws_access_key_id is not None:
        kwargs["aws_access_key_id"] = aws_access_key_id
        kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token is not None:
            kwargs["aws_session_token"] = aws_session_token
    return cast("TextractSDKClient", boto3.client("textract", **kwargs))
```

Implement `TextractClient(AuditedClientBase)` with constructor fields for region, shared SDK client, maximum response bytes, limiter, and token ID. `start_document_analysis()` must sort the already-validated feature tuple before both request and audit. `get_document_analysis()` must set `MaxResults=1000`, return the raw next token only to the orchestrator, replace it with a SHA-256 fingerprint in audit/telemetry, remove `ResponseMetadata`, and preserve the remaining semantic response.

Both methods allocate the call index before SDK invocation, acquire the limiter once, record exactly one success/error call, then emit telemetry. Use `canonical_json(semantic_response)` to enforce the per-call byte cap before constructing `RawCallPayload`.

- [x] **Step 5: Run client tests**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_client.py -q
```

Expected: PASS.

- [x] **Step 6: Commit Task 3**

```bash
git add src/elspeth/plugins/transforms/aws/textract_client.py tests/unit/plugins/transforms/aws/test_textract_client.py
git commit -m "feat(textract): audit asynchronous SDK calls"
```

---

### Task 4: Transform lifecycle, idempotency, polling, and projection

**Files:**

- Modify: `src/elspeth/plugins/transforms/aws/textract_document_analysis.py`
- Modify: `src/elspeth/contracts/errors.py`
- Modify: `config/cicd/contracts-whitelist.yaml`
- Modify: `tests/unit/plugins/transforms/aws/test_textract_document_analysis.py`
- Create: `tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py`

- [x] **Step 1: Write failing transform metadata and idempotency tests**

Add tests asserting the final class metadata, declared fields, canonical feature request order, and deterministic token behavior:

```python
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
```

Add parameterized row-input failures for missing bucket/key/version, wrong types, invalid bucket regex/length, blank key/version, key containing `#`, and overlong key/version.

- [x] **Step 2: Write failing polling/pagination/projection tests**

Use a fake audited client factory whose start returns `job-1` and whose get sequence is configurable. Cover:

- `IN_PROGRESS` then `SUCCEEDED`;
- two paginated successful pages;
- all configured projections;
- `FAILED`, `PARTIAL_SUCCESS`, and unknown status;
- poll timeout marked retryable;
- repeated `NextToken`;
- `max_result_pages`;
- shutdown during backoff;
- retryable `TextractServiceError` versus non-retryable service errors;
- `TextractIdempotencyInvariantError` mapped to `FrameworkBugError`; and
- no output emitted before the full result parser succeeds.

- [x] **Step 3: Run transform tests and verify plugin-class failures**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_document_analysis.py -q
```

Expected: failures because `AWSTextractDocumentAnalysis` is not defined.

- [x] **Step 4: Implement plugin metadata, construction, and schemas**

Add imports from the client/result modules and define:

```python
class AWSTextractDocumentAnalysis(BaseTransform, BatchTransformMixin):
    name = "aws_textract_document_analysis"
    determinism = Determinism.EXTERNAL_CALL
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:0000000000000000"
    config_model = AWSTextractDocumentAnalysisConfig
    passes_through_input = True
    creates_tokens = False
    audit_characteristics = frozenset({AuditCharacteristic.CREDENTIALS})
    capability_tags = ("aws", "textract", "document", "ocr", "enrichment")
```

In `__init__`, parse the config once, initialize declared input fields, copy secrets into private attributes only, compute the sorted feature tuple and immutable query request tuple, derive all declared output fields, build input/output schemas, and initialize lifecycle/client/batch state. Set `_effective_batch_wait_timeout_seconds` to the maximum of the configured batch wait and `poll_timeout_seconds + 90.0`.

- [x] **Step 5: Implement lifecycle and idempotency**

`on_start()` must require `ctx.landscape`, capture run/node identity and telemetry, obtain the plugin limiter, and lazily call `build_textract_sdk_client`. Do not build the SDK client in `__init__` or preflight mode.

Implement `_client_request_token()` by feeding a length-delimited list of UTF-8 fields to `hashlib.sha256`; include a domain string, plugin version, all execution/document identity, sorted feature types, and canonical JSON of query request dictionaries. Return `digest.hexdigest()`.

Implement `close()` in the approved order: set shutdown, shut down the batch mixin, clear row wrappers, close the shared SDK once, then clear recorder/client references.

- [x] **Step 6: Implement row orchestration and projection**

Implement `connect_output()`, `accept()`, the intentional `process()` error, `_process_row()`, `_process_single_with_state()`, `_poll_and_collect()`, input validation, backoff through `self._shutdown.wait(timeout=...)`, pagination token tracking, and error mapping.

Call `normalize_textract_result()` only after `NextToken` is exhausted. Build output from `row.to_dict()`, add only configured fields, then apply:

```python
output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output)
output_contract = self._apply_declared_output_field_contracts(output_contract)
output_contract = self._align_output_contract(output_contract)
```

Return success reason `action="enriched"`, sorted `fields_added`, and bounded metadata containing job ID, page/block counts, model version, warning count, feature list, and `result_status="succeeded"`.

- [x] **Step 7: Implement the no-network invariant probe**

`probe_config()` uses default-chain mode, synthetic bucket/key fields, `FORMS`, one text output, and observed schema. `forward_invariant_probe_rows()` adds the synthetic locator fields. `execute_forward_invariant_probe()` injects a fake shared SDK whose start returns `job-probe` and whose get returns one valid PAGE/LINE response; it exercises `_process_single_with_state()` and restores all prior lifecycle state in `finally`.

- [x] **Step 8: Add the ADR-009 contract test**

Create `tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py` mirroring the Azure Document Intelligence contract test. Assert external-call determinism, pass-through, schema presence, enrichment, preservation of pre-existing fields, and `success_reason["action"] == "enriched"`.

- [x] **Step 9: Run transform and contract tests**

Run:

```bash
pytest tests/unit/plugins/transforms/aws/test_textract_document_analysis.py tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py -q
```

Expected: PASS.

- [x] **Step 10: Commit Task 4**

```bash
git add src/elspeth/plugins/transforms/aws/textract_document_analysis.py tests/unit/plugins/transforms/aws/test_textract_document_analysis.py tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py
git commit -m "feat(textract): add asynchronous enrichment transform"
```

---

### Task 5: Discovery, catalog, and production secret-ref parity

**Files:**

- Modify: `tests/unit/web/execution/test_validation.py`
- Modify: `tests/unit/web/catalog/test_service.py`
- Create: `tests/golden/web/catalog/knob_schema/transform__aws_textract_document_analysis.json`

- [ ] **Step 1: Write failing discovery and catalog tests**

Add to `tests/unit/web/catalog/test_service.py`:

```python
def test_textract_transform_is_discoverable_with_credentials_characteristic(
    catalog: CatalogServiceImpl,
) -> None:
    transform = next(
        item for item in catalog.list_transforms()
        if item.name == "aws_textract_document_analysis"
    )
    assert "credentials" in transform.audit_characteristics
    schema = catalog.get_schema("transform", "aws_textract_document_analysis")
    names = {field.name for field in schema.config_fields}
    assert {"auth_mode", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"} <= names
```

Also assert `get_shared_plugin_manager().get_transform_by_name(...)` returns `AWSTextractDocumentAnalysis` and that assistance/example text contains S3, async analysis, and `{secret_ref: ...}` guidance without literal credentials.

- [ ] **Step 2: Add web secret-shape tests for all explicit AWS credential fields**

In `TestValidatePipelineFabricatedCredentials`, add a Textract node with literal `aws_access_key_id`, `aws_secret_access_key`, and `aws_session_token`; assert validation fails, names all three fields, and echoes none of the values. Add a positive node using three available `{secret_ref: ...}` markers; mock settings loading as the neighboring positive-control test does and assert the `secret_refs` check passes.

- [ ] **Step 3: Run focused catalog/web tests**

Run:

```bash
pytest tests/unit/web/catalog/test_service.py tests/unit/web/execution/test_validation.py -q
```

Expected: catalog discovery passes once the plugin class exists; secret-shape cases expose any central-policy or config-preflight drift.

- [ ] **Step 4: Generate the exact knob-schema snapshot**

Run this mechanical snapshot command:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager; from elspeth.web.catalog.service import CatalogServiceImpl; info=CatalogServiceImpl(get_shared_plugin_manager()).get_schema("transform", "aws_textract_document_analysis"); payload={"plugin_kind":"transform","plugin_name":"aws_textract_document_analysis","knob_schema":info.knob_schema}; Path("tests/golden/web/catalog/knob_schema/transform__aws_textract_document_analysis.json").write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n", encoding="utf-8")'
```

- [ ] **Step 5: Run the golden and built-in metadata tests**

Run:

```bash
pytest tests/unit/web/catalog/test_knob_schema_golden.py tests/unit/plugins/test_builtin_plugin_metadata.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add tests/unit/web/execution/test_validation.py tests/unit/web/catalog/test_service.py tests/golden/web/catalog/knob_schema/transform__aws_textract_document_analysis.json
git commit -m "test(textract): cover discovery and secret refs"
```

---

### Task 6: Production-path integration and opt-in live AWS acceptance

**Files:**

- Create: `tests/integration/plugins/transforms/aws/test_textract_document_analysis_pipeline.py`
- Create: `tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py`

- [ ] **Step 1: Write the production-path integration test**

Create a temporary observed-schema CSV source, Textract transform, and JSON sink settings document. Load it through the real settings loader, call `instantiate_plugins_from_config()`, and build the real execution graph. Monkeypatch only `build_textract_sdk_client` beneath the plugin boundary with a fake SDK that returns `IN_PROGRESS`, then two paginated `SUCCEEDED` responses.

Execute one row using the existing integration helper/orchestrator fixture. Assert:

- original bucket/key fields survive;
- text, table, form, metadata, and native outputs match the pure parser contract;
- the execution graph declares every configured output field;
- Landscape contains one start call and three get calls under the row state;
- call indices are unique and ordered;
- no credential value or raw `NextToken` appears in retrieved call payloads; and
- the output state contract is aligned and locked.

- [ ] **Step 2: Run the production-path integration test**

Run:

```bash
pytest tests/integration/plugins/transforms/aws/test_textract_document_analysis_pipeline.py -q
```

Expected: PASS.

- [ ] **Step 3: Add the opt-in live AWS test**

Create `tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py` with `@pytest.mark.live_aws`. Require gate `ELSPETH_RUN_LIVE_TEXTRACT=1` plus `ELSPETH_TEST_TEXTRACT_REGION`, `ELSPETH_TEST_TEXTRACT_BUCKET`, `ELSPETH_TEST_TEXTRACT_KEY`, and `ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT`. Skip if the gate is absent; fail with a static message if the gate is present but inputs are incomplete.

Use default-chain auth, `LAYOUT`, text and metadata outputs, an in-memory Landscape recorder, and the real plugin lifecycle. Assert success, expected text containment, positive page/block counts, and at least one audited start/get call. Failure messages must never render the document text, bucket/key, credentials, or raw AWS exception.

- [ ] **Step 4: Verify the live test is safely skipped by default**

Run:

```bash
pytest tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py -q
```

Expected: one skipped test and no AWS call.

- [ ] **Step 5: Commit Task 6**

```bash
git add tests/integration/plugins/transforms/aws/test_textract_document_analysis_pipeline.py tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py
git commit -m "test(textract): prove production and live paths"
```

---

### Task 7: Source hash, complete verification, and closeout

**Files:**

- Modify: `src/elspeth/plugins/transforms/aws/textract_document_analysis.py` (`source_file_hash` only)
- Modify only if generated by current gates: trust-tier allowlist/signature staging artifacts permitted by the existing judge workflow

- [ ] **Step 1: Regenerate the plugin source hash mechanically**

Run:

```bash
.venv/bin/python -c 'from pathlib import Path; from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash; path=Path("src/elspeth/plugins/transforms/aws/textract_document_analysis.py"); value=compute_source_file_hash(path); fix_source_file_hash(path, "AWSTextractDocumentAnalysis", value); print(value)'
```

Expected: prints `sha256:` followed by 16 lowercase hex characters and updates only the hash literal.

- [ ] **Step 2: Run all focused Textract and secret/catalog tests**

Run:

```bash
pytest \
  tests/unit/plugins/transforms/aws/test_textract_document_analysis.py \
  tests/unit/plugins/transforms/aws/test_textract_client.py \
  tests/unit/plugins/transforms/aws/test_textract_result.py \
  tests/property/plugins/transforms/aws/test_textract_result_properties.py \
  tests/unit/contracts/transform_contracts/test_aws_textract_document_analysis_contract.py \
  tests/integration/plugins/transforms/aws/test_textract_document_analysis_pipeline.py \
  tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py \
  tests/unit/core/test_resolve_secret_refs.py \
  tests/unit/web/catalog/test_service.py \
  tests/unit/web/catalog/test_knob_schema_golden.py \
  -q
```

Expected: PASS with only the ungated live AWS test skipped.

- [ ] **Step 3: Run the full CI-equivalent suite**

Run:

```bash
pytest tests/
```

Expected: PASS. The plain default selection is this repository's CI-equivalent suite.

- [ ] **Step 4: Run static, plugin-contract, and trust-tier gates**

Run:

```bash
elspeth-lints check
```

Expected: PASS. If the trust-tier rule alone reports new judge-gated findings, stage the current worklist key-free through `elspeth-judge`, have the operator sign it with `elspeth-lints sign-bundle`, then rerun this exact command. The implementation agent must never access `ELSPETH_JUDGE_METADATA_HMAC_KEY`.

- [ ] **Step 5: Run the external-input boundary gate**

Run:

```bash
wardline scan . --fail-on ERROR
```

Expected: exit 0. Fix any finding at the S3/provider response boundary and rerun focused tests before repeating the scan.

- [ ] **Step 6: Check worktree scope and diff integrity**

Run:

```bash
git status --short
git diff --check
git diff --stat release/0.7.2...HEAD
```

Expected: only Textract implementation, tests, generated catalog snapshot, plan/spec documentation, and any explicitly required judge staging artifacts appear; `git diff --check` prints nothing.

- [ ] **Step 7: Commit final generated hash or gate repairs**

```bash
git add src/elspeth/plugins/transforms/aws/textract_document_analysis.py
git commit -m "chore(textract): finalize plugin verification"
```

If no file changed after Task 6, do not create an empty commit.

- [ ] **Step 8: Record final evidence**

Capture the focused-test result, full-suite result, `elspeth-lints` result, Wardline exit status, commit list, and whether the live AWS test ran or skipped. Distinguish code-complete status from any operator-only signature or real-AWS acceptance action.
