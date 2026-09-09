# Amazon Textract Document Analysis Transform

**Date:** 2026-07-29
**Status:** Approved design
**Target:** ELSPETH `release/0.7.2`
**Plugin:** `aws_textract_document_analysis`

## Decision

Add an ELSPETH row-enrichment transform that analyzes documents already stored
in Amazon S3 through Textract's asynchronous `StartDocumentAnalysis` and
`GetDocumentAnalysis` APIs.

The transform accepts an S3 bucket, object key, and optional object version from
each input row. It submits one idempotent Textract job per row, polls the job,
retrieves every result page, validates the complete block graph, and enriches
the original row with configurable normalized projections. Operators may also
request a bounded provider-shaped aggregate containing the native Textract
blocks.

Synchronous inline-document processing is intentionally not part of this
plugin. A future `aws_textract_inline_analysis` plugin may reuse the pure result
parser and narrowly shared AWS utilities without adding an execution-mode switch
to this transform.

## Context

ELSPETH already has the patterns this integration needs:

- AWS SDK construction and default-chain authentication in the S3 and Bedrock
  plugins;
- row-level pipelining through `BatchTransformMixin`;
- one audited external-call record per provider operation;
- strict Tier 3 response validation;
- transform input/output field declarations and schema-contract propagation;
- deferred web secrets through `{secret_ref: NAME}` and CLI environment
  expansion through `${NAME}`; and
- an Azure Document Intelligence transform whose long-running-operation shape
  is similar, while its HTTP-specific implementation remains separate.

Textract's asynchronous API is the production-shaped first increment because it
supports multi-page PDF and TIFF documents as well as single-page JPEG, PNG,
TIFF, and PDF inputs. The document must be stored in S3. Textract returns a job
identifier from `StartDocumentAnalysis`; `GetDocumentAnalysis` then exposes job
status and paginated blocks.

## Goals

1. Analyze one immutable S3 document reference per input row.
2. Support the complete v1 `FeatureTypes` vocabulary: `TABLES`, `FORMS`,
   `QUERIES`, `SIGNATURES`, and `LAYOUT`.
3. Produce deterministic, documented projections for text, pages, tables,
   forms, queries, signatures, layout, and job metadata.
4. Preserve maximum provider fidelity through an optional bounded native-result
   field.
5. Record every start, poll, and pagination call in Landscape with row identity.
6. Prevent duplicate Textract jobs when ELSPETH retries a row.
7. Use ELSPETH's existing secret-reference, fingerprinting, redaction, and
   configuration-export infrastructure for explicit AWS credentials.
8. Fail malformed, partial, oversized, or unbounded provider responses closed at
   the row boundary.
9. Remain usable from YAML, CLI discovery, and the authenticated Web Composer.

## Non-goals

V1 does not include:

- synchronous `AnalyzeDocument` or inline document bytes;
- `DetectDocumentText` as a separate text-only mode;
- Textract Expense, Identity, Lending, or custom adapter APIs;
- Textract custom query adapters (`AdaptersConfig`);
- SNS completion notifications;
- customer-managed Textract result buckets (`OutputConfig`) or their KMS
  settings;
- a custom AWS endpoint URL;
- plugin-managed AWS credential storage or refresh;
- splitting one document into multiple ELSPETH rows; or
- releasing partial results from a `PARTIAL_SUCCESS` job.

## Public configuration contract

### Representative configuration

```yaml
transform:
  plugin: aws_textract_document_analysis
  options:
    region: ap-southeast-2

    auth_mode: default_chain

    bucket_field: document_bucket
    key_field: document_key
    version_field: document_version

    feature_types: [TABLES, FORMS, LAYOUT]
    queries: []

    text_field: textract_text
    page_count_field: textract_page_count
    metadata_field: textract_metadata
    extract:
      pages: textract_pages
      tables: textract_tables
      forms: textract_forms
      queries: textract_queries
      signatures: textract_signatures
      layout: textract_layout
    result_field: textract_native

    poll_interval_seconds: 1.0
    poll_backoff_multiplier: 1.5
    poll_max_interval_seconds: 10.0
    poll_timeout_seconds: 3600.0
    batch_wait_timeout_seconds: 3900.0

    max_result_pages: 1000
    max_blocks: 200000
    max_result_bytes: 50000000

    schema: {mode: observed}
```

### Configuration fields

| Field | Type and default | Contract |
|---|---|---|
| `region` | required string | Bounded AWS region identifier. The S3 object and Textract endpoint must be usable in this region. |
| `auth_mode` | `default_chain` or `secret_refs`; default `default_chain` | Selects exactly one credential source. |
| `aws_access_key_id` | optional secret string | Required only in `secret_refs` mode. |
| `aws_secret_access_key` | optional secret string | Required only in `secret_refs` mode. |
| `aws_session_token` | optional secret string | Optional in `secret_refs` mode; forbidden in `default_chain` mode. |
| `bucket_field` | required string | Input-row field containing the S3 bucket. |
| `key_field` | required string | Input-row field containing the S3 object key. |
| `version_field` | optional string | Input-row field containing the S3 object version. When configured, the field is required on every row. |
| `feature_types` | required list of 1-5 strings | Unique, exact members of the closed v1 feature vocabulary. |
| `queries` | list; default empty | At most 30 query objects. Non-empty only when `QUERIES` is selected. |
| `text_field` | optional string | Output field for page-ordered line text. |
| `page_count_field` | optional string | Output field for the validated page count. |
| `metadata_field` | optional string | Output field for bounded job/document metadata. |
| `extract` | object; all members optional | Maps each normalized facet to an output field name. |
| `result_field` | optional string | Output field for the bounded provider-shaped aggregate. |
| `poll_interval_seconds` | positive float; `1.0` | Initial status-poll delay. |
| `poll_backoff_multiplier` | float >= 1; `1.5` | Poll-delay multiplier. |
| `poll_max_interval_seconds` | positive float; `10.0` | Poll-delay ceiling. |
| `poll_timeout_seconds` | positive float; `3600.0` | Total elapsed deadline from submission through the terminal status. |
| `batch_wait_timeout_seconds` | positive float; `3900.0` | Batch-mixin wait bound; the effective value must cover the poll deadline and SDK headroom. |
| `max_result_pages` | positive integer; `1000` | Maximum `GetDocumentAnalysis` result pages, including the terminal poll response. |
| `max_blocks` | positive integer; `200000` | Maximum combined block count. |
| `max_result_bytes` | positive integer; `50000000` | Maximum canonical-JSON size of the combined semantic result. |
| `schema` | `SchemaConfig` | Existing ELSPETH transform schema contract. |

At least one of `text_field`, `page_count_field`, `metadata_field`,
`result_field`, or an `extract` member must be configured. Every configured
output name must be non-empty and unique.

`bucket_field`, `key_field`, and the optional `version_field` participate in
`declared_input_fields`. All configured output names participate in
`declared_output_fields`.

### Query configuration

Each query has this shape:

```yaml
queries:
  - text: What is the invoice total?
    alias: invoice_total
    pages: ["1-3", "5-*"]
```

Rules:

- `text` is required, 1-200 characters, and must satisfy the provider's allowed
  character contract.
- `alias` is optional and has the same length and character bounds.
- `pages` is optional. Each selector is 1-9 characters and matches
  `^[0-9*-]+$`.
- If a selector is `"*"`, the entire `pages` list must be exactly `["*"]`.
- Explicit intervals must have positive, ordered endpoints. A terminal `*` is
  allowed only as the interval end.
- The list contains no duplicates.
- V1 accepts at most 30 configured queries, matching Textract's asynchronous
  per-page query ceiling. Operators remain responsible for ensuring overlapping
  page selectors do not exceed the provider's per-page limit.
- Queries and the `QUERIES` feature are set together or both absent.

## AWS identity and secrets

### Default-chain mode

```yaml
auth_mode: default_chain
```

The plugin passes no explicit credentials to boto3. Boto3 resolves identity
through its normal chain, including workload/ECS roles, instance roles,
profiles, web identity, and environment credentials. This is the preferred
deployment mode because no long-lived credential enters pipeline
configuration.

All three explicit credential fields must be absent in this mode.

### Secret-reference mode

```yaml
auth_mode: secret_refs
aws_access_key_id:
  secret_ref: AWS_ACCESS_KEY_ID
aws_secret_access_key:
  secret_ref: AWS_SECRET_ACCESS_KEY
aws_session_token:
  secret_ref: AWS_SESSION_TOKEN
```

For web-authored runs, the existing `WebSecretService` resolves these markers
before plugin construction. For CLI-authored YAML, exact `${NAME}` values use
the existing settings environment expansion. The plugin receives ordinary
strings only after this boundary and does not call a secret store itself.

`aws_access_key_id` and `aws_secret_access_key` are required together. The
session token is optional. Empty values and partial credential tuples are
invalid.

### Secret-policy integration

The implementation must:

1. add `aws_access_key_id` to the central credential-field recognition policy;
2. rely on the existing `_key` and `_token` suffix policy for the other fields;
3. mark all three Pydantic fields `repr=False` and enable input hiding for their
   model;
4. ensure web validation rejects literal credential values and permits only
   approved deferred-secret markers;
5. preserve unresolved markers in exported public YAML;
6. include only HMAC/fingerprint derivatives in configuration identity and
   audit metadata; and
7. declare `AuditCharacteristic.CREDENTIALS` on the plugin.

Plaintext credential values must never enter Landscape request/response data,
row output, success reasons, telemetry, logs, exception text, validation text,
catalog metadata, or exported configuration.

## Plugin architecture

### `aws_textract_document_analysis`

The transform owns configuration, schema declarations, lifecycle, row-level
batching, input validation, orchestration, and result projection.

Required plugin metadata:

- `name = "aws_textract_document_analysis"`
- `determinism = Determinism.EXTERNAL_CALL`
- `plugin_version = "1.0.0"`
- `passes_through_input = True`
- `creates_tokens = False`
- `audit_characteristics = {AuditCharacteristic.CREDENTIALS}`
- capability tags covering AWS, Textract, document analysis, OCR, and
  enrichment

The class implements `BaseTransform` plus `BatchTransformMixin`. Its ordinary
`process()` path is unavailable; the engine uses `connect_output()` and
`accept()` so multiple documents may be in flight while output remains FIFO.

### Audited Textract client

A focused client adapter wraps one shared boto3 Textract client. It owns:

- exact `StartDocumentAnalysis` and `GetDocumentAnalysis` request construction;
- one Landscape call record per SDK operation;
- rate-limiter acquisition before every SDK call;
- response size measurement;
- SDK error classification;
- safe telemetry after successful audit persistence; and
- redaction of provider-controlled failure text.

The boto3 client uses botocore standard retries with three total attempts, a
10-second connect timeout, and a 30-second read timeout. The adapter does not add
another per-call retry loop.

The SDK client is created in `on_start()` and closed once during plugin
shutdown. Each row gets a small audited wrapper bound to its `state_id` and
`token_id`; wrappers share the thread-safe SDK client but never share audit
parentage.

### Pure result parser

A pure Textract-result module validates and normalizes the combined semantic
response. It imports no boto3, Landscape, engine, or plugin-lifecycle types.

The parser owns:

- block indexing and duplicate-ID detection;
- relationship resolution;
- page membership and deterministic ordering;
- text assembly;
- table reconstruction;
- form key/value reconstruction;
- query/result matching;
- signature and layout projection;
- page-count and model-metadata validation; and
- construction of the provider-shaped aggregate.

Known members used by normalization are strictly type-checked. Unknown members
inside otherwise valid blocks are preserved in those blocks when `result_field`
is enabled, but they do not silently alter normalized projections. Unknown
top-level response members remain available in the per-call audit payload and
are omitted from the combined row aggregate because their cross-page merge
semantics are undefined. A malformed known member or a dangling referenced
block fails the row closed.

## Per-row data flow

1. Require a Landscape recorder, run identity, row state identity, token
   identity, and an initialized SDK client.
2. Read and validate the configured input fields.
3. Build `DocumentLocation.S3Object` from `Bucket`, `Name`, and optional
   `Version`.
4. Build the canonical request-affecting identity document.
5. Derive the idempotency token.
6. Call `StartDocumentAnalysis` and validate the returned `JobId`.
7. Poll `GetDocumentAnalysis(JobId, MaxResults=1000)` using bounded exponential
   backoff until a terminal state.
8. On `SUCCEEDED`, retain that terminal response as result page one.
9. Follow `NextToken` until exhausted, always using `MaxResults=1000`.
10. Enforce result-page, block-count, serialized-byte, elapsed-time, and
    repeated-token bounds incrementally.
11. Validate the complete response graph.
12. Build only the configured normalized projections and optional native
    aggregate.
13. Propagate and align the input/output schema contract.
14. Emit exactly one enriched row or one row-level error.

The transform never submits all rows and then waits as a barrier. Each worker
owns one row's submit/poll/retrieve sequence. `BatchTransformMixin` supplies
backpressure and FIFO release.

## Idempotency

`StartDocumentAnalysis.ClientRequestToken` is the lower-case hexadecimal
SHA-256 digest of a canonical, length-delimited identity containing:

- a plugin-domain separator and plugin version;
- run ID;
- node ID;
- token ID;
- region;
- bucket, key, and optional object version;
- feature types in canonical sorted order;
- queries in configured order, including aliases and page selectors; and
- every future option that changes the `StartDocumentAnalysis` request.

The 64-character digest satisfies Textract's token length and character rules.
The raw identity inputs are not recoverable from the token.

The actual `FeatureTypes` request list uses the same canonical sorted order.
Otherwise two configurations that differ only in list order could derive the
same token while sending bytewise-different request parameters to AWS.

Retries of the same row in the same run therefore receive the same Textract
`JobId`. A document or analysis-setting change produces a different token.

`IdempotentParameterMismatchException` is a framework invariant failure. With a
correct identity document, the same token cannot accompany different request
parameters. Treating this as an ordinary row failure would hide an
implementation or execution-identity defect.

## Polling and pagination

The poll loop starts at `poll_interval_seconds`, multiplies by
`poll_backoff_multiplier`, and caps at `poll_max_interval_seconds`. Waiting uses
the plugin shutdown event rather than uninterruptible sleep.

The total poll deadline is anchored immediately before submission. Submission
latency therefore consumes the same `poll_timeout_seconds` budget as subsequent
polls. Batch wait time is raised to at least the poll budget plus SDK timeout
headroom.

Terminal handling:

- `SUCCEEDED`: retrieve and normalize the complete result.
- `FAILED`: fail the row with `analysis_failed`.
- `PARTIAL_SUCCESS`: fail the row with `partial_success`; record bounded warning
  codes/pages but release no partial output.
- `IN_PROGRESS`: continue until the deadline.
- any other value: `malformed_response`.

Pagination maintains a set of seen `NextToken` values. Repetition yields
`pagination_cycle`. More than `max_result_pages` pages yields
`pagination_limit_exceeded`, even if every token is unique.

## Landscape call contract

Every SDK invocation records one call with `CallType.HTTP` and provider
`aws_textract`.

### Submission request

The audited request contains:

- operation `start_document_analysis`;
- region;
- bucket, key, and optional version;
- feature types in canonical request order;
- query configuration;
- idempotency-token fingerprint; and
- no credential fields or AWS signing material.

The successful response contains the bounded `JobId`, whether an AWS request ID
was present, and the SDK attempt count.

### Poll and result requests

The audited request contains:

- operation `get_document_analysis`;
- bounded `JobId`;
- whether a pagination token was supplied;
- a one-way pagination-token fingerprint when present; and
- `MaxResults=1000`.

The successful response stores the semantic provider response after removing
SDK `ResponseMetadata`. This includes status, document metadata, warnings,
model version, blocks, and the presence/fingerprint of a next token.

Call payloads use Landscape's existing content-addressed payload storage; the
plugin creates no parallel blob store. If `result_field` is enabled, the row's
provider-shaped aggregate may repeat document content in row-state storage.
That cost is explicit operator opt-in and is why the field is disabled by
default and bounded.

### Failure records

Failure audit data contains only:

- ELSPETH-owned error category;
- bounded AWS error code;
- retryable flag;
- attempt count; and
- operation name.

Raw exception strings, AWS status messages, response headers, and document text
must not be copied into errors or logs.

Telemetry is emitted only after the corresponding Landscape record succeeds.
Telemetry carries stable hashes and bounded summaries, never raw blocks or
credentials.

## Normalized output contract

### Text

`text_field` contains `LINE.Text` values in provider arrival order within each
validated page. Lines are joined with `\n`. Pages are ordered by page number and
joined with `\n\f\n`. No geometry-based reordering is invented locally.

### Page count

`page_count_field` contains an integer. `DocumentMetadata.Pages` must agree with
the count and numbering of `PAGE` blocks in a successful complete response.

### Pages

Each `extract.pages` item has:

```json
{
  "page": 1,
  "text": "page text",
  "geometry": {},
  "lines": [
    {
      "id": "line-block-id",
      "text": "line text",
      "confidence": 99.1,
      "geometry": {}
    }
  ]
}
```

Geometry retains Textract's bounding-box and polygon representation after type
and numeric-range validation.

### Tables

Each `extract.tables` item has:

```json
{
  "id": "table-block-id",
  "page": 1,
  "confidence": 98.5,
  "geometry": {},
  "entity_types": [],
  "rows": [
    [
      {
        "id": "cell-block-id",
        "row": 1,
        "column": 1,
        "row_span": 1,
        "column_span": 1,
        "text": "cell text",
        "confidence": 98.0,
        "geometry": {},
        "selection_status": null
      }
    ]
  ]
}
```

Rows and columns are one-based as reported by Textract. Sparse tables preserve
their coordinates; the parser does not shift cells to close gaps. Provider
structures not represented by this normalized shape remain available through
`result_field`.

### Forms

Each `extract.forms` item has:

```json
{
  "page": 1,
  "key": "Invoice number",
  "value": "INV-123",
  "key_block_id": "key-id",
  "value_block_id": "value-id",
  "key_confidence": 97.0,
  "value_confidence": 96.0,
  "key_geometry": {},
  "value_geometry": {}
}
```

A valid key without a matched value emits `null` value members rather than
failing. A relationship that names a block absent from the complete result is
malformed.

### Queries

Each `extract.queries` item has:

```json
{
  "page": 1,
  "query": "What is the invoice total?",
  "alias": "invoice_total",
  "answer": "$42.00",
  "confidence": 95.0,
  "query_block_id": "query-id",
  "answer_block_id": "answer-id"
}
```

An unanswered query is valid and emits `null` answer, confidence, and answer ID.

### Signatures

Each `extract.signatures` item contains block ID, page, confidence, and geometry.

### Layout

Each `extract.layout` item contains block ID, exact Textract layout block type,
page, text reconstructed from child relationships, confidence, and geometry.
Items retain provider arrival order within a page.

### Metadata

`metadata_field` contains:

```json
{
  "job_id": "provider-job-id",
  "job_status": "SUCCEEDED",
  "page_count": 2,
  "block_count": 412,
  "model_version": "provider-model-version",
  "warnings": [],
  "feature_types": ["FORMS", "TABLES"],
  "s3_version": "optional-version"
}
```

Warnings contain bounded provider error codes and page-number lists, never raw
status messages.

### Provider-shaped result

`result_field` contains one aggregate with provider key casing:

```json
{
  "JobStatus": "SUCCEEDED",
  "DocumentMetadata": {"Pages": 2},
  "AnalyzeDocumentModelVersion": "provider-model-version",
  "Warnings": [],
  "Blocks": []
}
```

The aggregate concatenates blocks in API response order. It excludes
`ResponseMetadata`, `NextToken`, and unknown top-level response members whose
cross-page merge semantics are undefined. Unknown members inside individual
blocks are preserved if the aggregate fits the result byte bound.

## Input and response validation

Input-row validation is separate from configuration validation:

- Bucket values are 3-255 characters and match Textract's
  `[0-9A-Za-z.\-_]*` contract.
- Object keys are 1-1,024 characters, contain at least one non-whitespace
  character, and do not contain `#`, which Textract rejects. Leading or
  trailing spaces are not stripped because they may be part of the S3 key.
- Object versions are 1-1,024 characters and contain at least one
  non-whitespace character when configured.
- Non-string values fail with `invalid_input` and an `actual_type`; values are
  never coerced.

Response validation occurs before any external value reaches an output row.
The parser rejects:

- missing or wrongly typed required members;
- duplicate block IDs;
- dangling referenced block IDs;
- invalid page numbers or page-count disagreement;
- invalid row/column/span values;
- non-finite or out-of-range confidence/geometry numbers;
- unknown job statuses;
- cyclic pagination; and
- configured resource-bound violations.

Unrecognized future block types do not fail an otherwise valid native aggregate,
but they do not silently contribute to normalized facets.

## Error and retry policy

### Retryable

- endpoint and connection failures;
- connect/read timeouts;
- `InternalServerError`;
- `ThrottlingException`;
- `ProvisionedThroughputExceededException`;
- concurrent-job `LimitExceededException`; and
- the ELSPETH-owned `poll_timeout` result.

Botocore owns retries inside one SDK call. After its three attempts are
exhausted, ELSPETH may retry the row. The deterministic request token reconnects
the row to the existing job.

### Non-retryable

- missing, empty, or wrongly typed inputs;
- `AccessDeniedException`;
- `InvalidS3ObjectException`;
- `BadDocumentException`;
- `UnsupportedDocumentException`;
- `DocumentTooLargeException`;
- invalid request parameters;
- invalid or expired job IDs;
- terminal `FAILED` or `PARTIAL_SUCCESS`;
- malformed block graphs;
- repeated pagination tokens; and
- configured count/size/page bounds.

### Framework invariant

`IdempotentParameterMismatchException` raises a framework-level invariant error.
It is neither retryable nor a normal row-data failure.

### Stable row reasons

- `missing_field`
- `invalid_input`
- `submit_failed`
- `poll_failed`
- `poll_timeout`
- `analysis_failed`
- `partial_success`
- `pagination_cycle`
- `pagination_limit_exceeded`
- `too_many_blocks`
- `result_too_large`
- `malformed_response`
- `shutdown_requested`

## Lifecycle and cancellation

`on_start()` captures the Landscape writer, run/node identity, telemetry
callback, rate limiter, shutdown event, and shared SDK client. It fails with a
framework error if required audit infrastructure is absent.

`connect_output()` initializes the batch mixin once. `accept()` delegates each
row to `_process_row()`. `process()` raises because this is a pipelined
transform.

`close()`:

1. sets the plugin shutdown event;
2. stops accepting new work;
3. shuts down batch processing through the mixin;
4. clears row-scoped audited wrappers;
5. closes the shared boto3 client exactly once; and
6. drops retained audit/lifecycle references.

All poll and backoff waits observe cancellation. Cancellation produces
`shutdown_requested` for affected rows and releases no partial output.

## Registration and discovery

The plugin must be registered as a built-in transform and exposed through:

- `elspeth plugins list` and `elspeth plugins describe`;
- runtime plugin construction;
- Web Composer catalog and knob schema;
- plugin assistance text and example YAML;
- the standard `aws` optional dependency extra; and
- source-file hash verification.

No new dependency or lockfile change is expected because boto3 and botocore are
already supplied by the `aws` extra.

The no-network probe uses a synthetic S3 reference and a fake audited SDK
adapter. Plugin construction and preflight must not contact AWS or resolve the
default credential chain eagerly.

## Testing strategy

### Configuration

Test valid minimal and full configurations plus:

- all auth-mode combinations;
- partial or literal credential failures;
- feature/query coupling;
- invalid query text and page selectors;
- duplicate features and output names;
- missing output targets;
- invalid polling relationships; and
- every resource bound.

### Secrets

Use the production web validation and resolution path to prove:

- user/server secret refs resolve before plugin construction;
- literal web credentials are rejected;
- missing refs fail with the established structured code;
- exported YAML retains markers rather than plaintext;
- default-chain mode needs no stored secret;
- explicit fields do not coexist with default-chain mode; and
- raw credential values are absent from validation errors, configuration
  fingerprints, audit payloads, telemetry, and logs.

### Pure parser

Use synthetic, minimal provider fixtures for every supported block family.
Cover:

- multi-page text;
- shuffled block order with provider-order preservation;
- sparse and spanned table cells;
- selection elements;
- form keys with and without values;
- answered and unanswered queries;
- signatures and layout blocks;
- geometry and confidence validation;
- duplicate IDs, dangling references, and malformed relationships;
- page-count disagreement; and
- unknown future block types in the native aggregate.

Property tests should permute dictionary insertion order and pagination cuts to
prove normalization is stable for an equivalent provider sequence.

### Audited client

Assert:

- exact boto3 request arguments;
- stable and request-sensitive idempotency tokens;
- one call record per submission, poll, and result page;
- audit-before-telemetry ordering;
- correct state/token attribution under concurrency;
- rate-limiter use;
- SDK attempt extraction;
- error allowlist classification;
- provider-message redaction;
- response-size handling; and
- single SDK-client closure.

### Transform and contracts

Assert:

- selected projections only;
- native-result opt-in only;
- schema declaration and propagation;
- FIFO output despite out-of-order worker completion;
- row-level failure isolation;
- backpressure and cancellation;
- forward/backward ADR-009 invariant probes; and
- stable success and error metadata.

### Production-path integration

At least one integration test must:

1. load real settings;
2. call `instantiate_plugins_from_config()`;
3. build the production execution graph;
4. inject a fake boto3 transport beneath the real plugin/client boundary;
5. run a row through submission, polling, and pagination;
6. assert the enriched output contract; and
7. read back Landscape call and payload records.

### Catalog and packaging

Cover built-in registration, CLI discovery, assistance text, audit
characteristics, credential-field policy, generated schema, and the knob-schema
golden. Verify the plugin fails clearly when the `aws` extra is unavailable.

### Live AWS acceptance

Add a `live_aws`-marked integration test. It is skipped unless the operator
supplies a region, bucket, object key, and expected fixture assertions. It uses
existing AWS identity conventions, performs no resource provisioning or
deletion, and must never print credentials or document contents on failure.

The live check proves real submission, at least one status poll, complete result
retrieval, normalized output, and Landscape call persistence. Pagination may be
proved in deterministic integration tests if the operator fixture fits in one
AWS result page.

## Completion gates

Implementation is complete only when:

1. focused unit, property, integration, catalog, and contract tests pass;
2. the production-path secret-ref test passes;
3. the generated plugin source hash and knob-schema golden are current;
4. `pytest tests/` passes;
5. `elspeth-lints check` passes;
6. `wardline scan . --fail-on ERROR` passes because S3 fields and provider
   responses are external-input boundaries; and
7. any trust-tier allowlist changes use the existing key-free staging and
   operator-signing workflow.

## Compatibility and future work

This is a new plugin and changes no existing runtime configuration. Its
credential-field policy addition is intentionally global so all web validation,
fingerprinting, and export surfaces agree that `aws_access_key_id` is
credential-bearing.

A future synchronous plugin may reuse:

- provider response parsing;
- normalized projection models;
- audited SDK response/error helpers; and
- AWS credential construction.

It must define its own input contract, limits, lifecycle, and plugin name. The
asynchronous plugin will not gain an `execution_mode` discriminator later.

## AWS references

- [Processing documents asynchronously](https://docs.aws.amazon.com/textract/latest/dg/async.html)
- [`StartDocumentAnalysis`](https://docs.aws.amazon.com/textract/latest/APIReference/API_StartDocumentAnalysis.html)
- [`GetDocumentAnalysis`](https://docs.aws.amazon.com/textract/latest/APIReference/API_GetDocumentAnalysis.html)
- [Textract set quotas](https://docs.aws.amazon.com/textract/latest/dg/limits-document.html)
- [Textract query shape](https://docs.aws.amazon.com/textract/latest/APIReference/API_Query.html)
- [Textract S3 object shape](https://docs.aws.amazon.com/textract/latest/APIReference/API_S3Object.html)
