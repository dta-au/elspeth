# Amazon Textract Inline Analysis Transform

**Date:** 2026-07-29
**Status:** Approved design
**Target:** ELSPETH `release/0.7.2`
**Transform plugin:** `aws_textract_inline_analysis`
**Companion source plugin:** `blob_rows`

## Decision

Add a synchronous Amazon Textract row-enrichment transform for documents held
as ELSPETH payload-store blobs. The Web Composer may accept one or more pasted
images, place each image under normal blob custody, and create a source that
emits one row per blob. The transform retrieves the bytes by content hash and
calls Textract `AnalyzeDocument` once for each row.

The Composer may determine a document's format while authoring the pipeline,
but it must persist that decision as the transform's required
`document_format` setting. Runtime code never infers the request format from a
filename, MIME type, or byte signature. It treats the configured format as the
operator-reviewed instruction and uses the byte signature only to verify that
the document agrees with it.

The transform supports JPEG, PNG, and explicitly configured single-page PDF
documents. It reuses the asynchronous Textract plugin's strict block-graph
parser and normalized projections, but it remains a separate plugin with no
execution-mode switch, job submission, polling, pagination, or S3 input.

The existing Composer source surface cannot currently express this flow:
managed blob MIME types are data/text oriented, and `set_source_from_blob`
binds one blob to a text, CSV, or JSON parser. This design therefore includes a
narrow, generic `blob_rows` source and the minimum binary-document custody
extension needed to emit authoritative blob references as rows. The source is
not Textract-specific and never copies document bytes into row data.

## Context

ELSPETH already has the relevant implementation patterns:

- content-addressed `PayloadStore` storage with integrity-checked retrieval;
- Composer blob ownership, readiness, retention, run-link, and source-binding
  controls;
- `blob_fetch`, which stores an HTTP response once and emits its payload hash;
- `blob_csv_expand`, which consumes a payload hash through the injected
  `PayloadStore` rather than carrying bytes in a row;
- AWS SDK construction and default-chain authentication in existing AWS
  plugins;
- deferred web secrets through `{secret_ref: NAME}` and CLI environment
  expansion through `${NAME}`;
- one audited external-call record per provider operation;
- strict Tier 3 response validation and schema-contract propagation; and
- row-level pipelining through `BatchTransformMixin`.

Amazon Textract documents `AnalyzeDocument` as a synchronous operation that
returns the complete result directly. Its synchronous processing guide covers
single-page JPEG, PNG, PDF, and TIFF documents. The generic `Document.Bytes`
reference is more restrictive, describing a 5 MB JPEG/PNG input even though
its shape permits up to 10 MiB and the operation-specific page describes a 10
MB synchronous error threshold. V1 resolves that documentation inconsistency
conservatively: it supports only the three approved formats, uses a hard 5 MiB
local byte ceiling, and requires live AWS acceptance coverage for inline PDF
before release.

## Goals

1. Analyze one payload-store document per input row with synchronous
   `AnalyzeDocument`.
2. Let Composer turn one pasted image into one source row and several pasted
   images into several rows without placing bytes or base64 in those rows.
3. Require an explicit, configuration-bound `jpeg`, `png`, or `pdf` format.
4. Support the complete v1 `FeatureTypes` vocabulary: `TABLES`, `FORMS`,
   `QUERIES`, `SIGNATURES`, and `LAYOUT`.
5. Reuse the asynchronous plugin's deterministic projections for text, pages,
   tables, forms, queries, signatures, and layout.
6. Preserve maximum provider fidelity through an optional bounded native
   result.
7. Record every provider invocation in Landscape without duplicating source
   bytes in the call record.
8. Use ELSPETH's existing secret-reference, fingerprinting, redaction, and
   public-export infrastructure for explicit AWS credentials.
9. Fail missing, corrupt, mismatched, oversized, multipage, malformed, or
   unbounded input and output closed at the row boundary.
10. Remain usable from YAML, CLI discovery, and the authenticated Web Composer.

## Non-goals

V1 does not include:

- S3 input, `StartDocumentAnalysis`, polling, result pagination, or
  provider-side idempotency tokens;
- TIFF input;
- multipage PDF processing;
- a direct base64 row-input mode;
- embedding document bytes in source options, row state, call metadata,
  telemetry, or errors;
- local PDF parsing or a PDF page-count dependency;
- `DetectDocumentText` as a separate text-only mode;
- Textract Expense, Identity, Lending, or custom adapter APIs;
- `AdaptersConfig` or `HumanLoopConfig`;
- a custom AWS endpoint URL;
- plugin-managed credential storage or refresh;
- splitting one document into several rows; or
- merging several pasted images into one document.

## End-to-end authoring contract

### One pasted image

1. The authenticated web boundary receives the binary image.
2. Blob custody validates its declared MIME type, size, and signature, stores
   the content, and marks one session-owned blob ready.
3. Composer binds that blob to a `blob_rows` source.
4. The source emits one row containing the payload hash and bounded metadata.
5. Composer sets `document_format` explicitly on
   `aws_textract_inline_analysis`.
6. The transform retrieves and analyzes the bytes.

### Several pasted images

Each image becomes a distinct managed blob and a distinct source row. If all
documents share one configured format, one `blob_rows` source and one Textract
node process them. If formats differ, Composer creates homogeneous named
sources or branches and one explicitly configured Textract node per format.
The runtime transform does not dispatch on per-row MIME values.

### Authority boundaries

The following facts come from different authorities and must not be conflated:

- The authenticated upload/paste boundary owns binary ingestion and the blob's
  declared MIME type.
- Composer owns the authoring-time interpretation that becomes
  `document_format`.
- Blob storage owns the content hash and byte length.
- The transform configuration owns request interpretation at runtime.
- Byte-signature validation proves agreement; it does not choose a format.
- Textract's validated response proves the successful document had exactly one
  page.

## Binary-document blob custody

### MIME vocabulary

The storage-level managed-blob MIME vocabulary gains:

- `image/jpeg`
- `image/png`
- `application/pdf`

The existing data/text MIME vocabulary remains unchanged. Code should expose
separate derived closed sets for text blobs, binary document blobs, and their
storage-level union rather than making every text consumer accept binary data.

The LLM-facing `create_blob` and `update_blob` tools remain text-only because
their public `content` field is a string. They must not acquire an implicit
base64 convention. Binary documents enter through the authenticated
paste/upload boundary, which receives bytes and performs the binary checks in
this specification.

Any database MIME `CHECK`, API literal, upload allowlist, blob DTO, frontend
accept list, and runtime Tier 1 read guard must derive from or remain aligned
with the same closed storage vocabulary. Existing text decoders continue to
reject binary MIME types.

### Admission checks

Before a binary document blob becomes `ready`, the web boundary must:

1. require a non-empty body;
2. enforce the existing upload limit and the 5 MiB Textract-inline ceiling for
   this authoring path;
3. accept only the three binary document MIME values;
4. verify MIME/signature agreement with the same exact signature rules used by
   the transform;
5. compute the SHA-256 content hash from the received bytes;
6. persist through the existing idempotent blob lifecycle; and
7. record normal creator, modality, message, model, and argument provenance.

Admission does not establish that a PDF has one page. V1 deliberately avoids
parsing untrusted PDFs locally. The successful Textract response is the
page-count authority; provider rejection or any returned page count other than
one fails the row.

## `blob_rows` companion source

### Purpose

`blob_rows` is a generic source for emitting managed blob references and
metadata without parsing or copying blob content. It supports binary document
workflows beyond Textract and keeps the transform row-oriented.

Required plugin metadata:

- `name = "blob_rows"`
- `determinism = Determinism.IO_READ`
- `plugin_version = "1.0.0"`
- `creates_tokens = True`
- capability tags covering blob, payload, binary, and source

### Persisted source options

Composer persists a non-empty, bounded `blobs` list. Every entry is populated
by the trusted blob resolver, not copied from LLM assertions:

```yaml
source:
  plugin: blob_rows
  on_success: documents
  options:
    blobs:
      - blob_id: 11111111-1111-1111-1111-111111111111
        payload_ref: 64-lowercase-hex-content-hash
        filename: page-1.png
        mime_type: image/png
        size_bytes: 183421
    schema: {mode: observed}
```

The list contains at most 1,000 unique blob IDs and unique payload hashes.
Entries preserve authoring order, which becomes source-row order. Field names
are fixed; v1 does not add output-field renaming options.

For web-authored pipelines, direct `set_source` calls may not populate or
modify `blobs`. A new plural authoritative binding path, exposed to Composer as
`set_source_from_blobs`, accepts blob IDs and resolves all remaining fields
from session-owned records. It performs the same ownership, readiness,
canonical-storage, pending-proposal, source-authoring, and review checks as the
existing singular binding path, atomically for the complete list.

The plural resolver must fail the whole mutation before changing composition
state if any ID is malformed, missing, duplicated, foreign, non-ready,
unsupported, or inconsistent. It records only bounded blob metadata in tool
audit, never storage paths or content. It does not UTF-8 decode binary blobs;
when a source-interpretation review is required, the review uses the existing
user-visible blob identity and bounded metadata rather than inventing a text
representation of the document.

### Runtime validation and rows

Before run creation, web execution re-resolves every persisted entry against
the session's authoritative blob record. Blob ID, payload hash, filename, MIME
type, byte length, readiness, and canonical storage identity must all agree.
All blobs are linked to the run as inputs before source execution. A mismatch
fails admission before any Textract call.

For each entry, `blob_rows` emits exactly one row:

```json
{
  "blob_id": "11111111-1111-1111-1111-111111111111",
  "blob_ref": "64-lowercase-hex-content-hash",
  "blob_filename": "page-1.png",
  "blob_mime_type": "image/png",
  "blob_size_bytes": 183421
}
```

`blob_ref` is the content-addressed `PayloadStore` reference consumed by the
Textract transform. `blob_id` is the web custody identity. The source does not
retrieve the blob body merely to emit the row; admission already proved the
authoritative binding, and the consuming transform performs integrity-checked
retrieval immediately before the external call.

Trusted CLI-authored use is allowed only when every configured payload ref
already exists in the configured `PayloadStore`; the source validates
existence before emitting rows. Outside the web service, `blob_id` is retained
as provenance supplied by the trusted operator and is not treated as proof of
session ownership. Web UUID ownership rules remain enforced exclusively by the
web admission layer.

## Transform public configuration contract

### Representative configuration

```yaml
transform:
  plugin: aws_textract_inline_analysis
  options:
    region: ap-southeast-2

    auth_mode: default_chain

    blob_ref_field: blob_ref
    document_format: png

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

    max_document_bytes: 5242880
    max_blocks: 100000
    max_result_bytes: 50000000
    request_timeout_seconds: 120
    batch_wait_timeout_seconds: 420

    schema: {mode: observed}
```

### Configuration fields

| Field | Type and default | Contract |
|---|---|---|
| `region` | required string | Bounded AWS region identifier. |
| `auth_mode` | `default_chain` or `secret_refs`; default `default_chain` | Selects exactly one credential source. |
| `aws_access_key_id` | optional secret string | Required only in `secret_refs` mode. |
| `aws_secret_access_key` | optional secret string | Required only in `secret_refs` mode. |
| `aws_session_token` | optional secret string | Optional in `secret_refs` mode; forbidden in `default_chain` mode. |
| `blob_ref_field` | string; `blob_ref` | Required input-row field containing a payload-store SHA-256 hash. |
| `document_format` | required `jpeg`, `png`, or `pdf` | Explicit request interpretation for every row handled by this transform instance. |
| `feature_types` | required list of 1-5 strings | Unique exact members of the closed Textract feature vocabulary. |
| `queries` | list; empty | At most 15 single-page query objects. Non-empty only with `QUERIES`. |
| `text_field` | optional string | Output field for page-ordered line text. |
| `page_count_field` | optional string | Output field for the validated page count, always `1` on success. |
| `metadata_field` | optional string | Output field for bounded document and model metadata. |
| `extract` | object; all members optional | Maps normalized facets to output field names. |
| `result_field` | optional string | Output field for the bounded provider-shaped response. |
| `max_document_bytes` | integer; `5242880` | Positive local byte bound; may be reduced but never raised above 5 MiB. |
| `max_blocks` | integer; `100000` | Maximum response block count. |
| `max_result_bytes` | integer; `50000000` | Maximum canonical-JSON size of the semantic response and native aggregate. |
| `request_timeout_seconds` | positive float; `120` | SDK read timeout for `AnalyzeDocument`; connect timeout remains 10 seconds. |
| `batch_wait_timeout_seconds` | positive float; `420` | Batch-mixin row wait bound. Effective wait covers all SDK attempts and headroom. |
| `schema` | `SchemaConfig` | Existing ELSPETH transform schema contract. |

At least one of `text_field`, `page_count_field`, `metadata_field`,
`result_field`, or an `extract` member must be configured. Every configured
output field name must be non-empty and unique. `blob_ref_field` participates
in `declared_input_fields`; every configured output name participates in
`declared_output_fields`.

### Format contract

`document_format` is required even when the row contains MIME metadata. The
format is fixed for the transform instance and is included in configuration
identity. Composer may map a familiar `.jpg` label to the canonical `jpeg`
enum while authoring, but runtime accepts no `jpg` alias.

After retrieving the bytes, the transform verifies exact agreement:

- `jpeg`: bytes begin `FF D8 FF`;
- `png`: bytes begin `89 50 4E 47 0D 0A 1A 0A`;
- `pdf`: bytes begin `%PDF-` at offset zero.

Leading whitespace, a byte-order mark, an embedded signature later in the
payload, or a different known signature is a non-retryable mismatch. Signature
validation is deliberately narrow and is not a general file-validity parser.

### Query configuration

Each query has this shape:

```yaml
queries:
  - text: What is the invoice total?
    alias: invoice_total
    pages: ["1"]
```

Rules:

- `text` is required, 1-200 characters, and satisfies Textract's allowed
  character pattern.
- `alias` is optional and has the same bounds.
- `pages` may be absent, `["1"]`, or `["*"]`; no other selector is valid for
  this single-page plugin.
- The list has at most 15 queries, Textract's synchronous per-page ceiling.
- Queries and the `QUERIES` feature are configured together or both absent.
- Query order is preserved. Feature types are sent in canonical sorted order.

## AWS identity and secrets

### Default-chain mode

```yaml
auth_mode: default_chain
```

The transform passes no explicit credentials to boto3. Boto3 resolves identity
through its normal chain, including workload/ECS roles, instance roles,
profiles, web identity, and environment credentials. All explicit credential
fields must be absent.

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

Web runs use the existing `WebSecretService` boundary. CLI-authored YAML uses
the existing exact `${NAME}` environment expansion. The transform receives
ordinary strings only after resolution and never calls a secret store itself.
Access key ID and secret access key are required together; session token is
optional. Empty values and partial tuples are invalid.

The inline and asynchronous Textract transforms share one credential config
and SDK-construction helper. The implementation must use the same central
credential recognition policy used by LLM and other credential-bearing
plugins, mark credential fields `repr=False`, enable Pydantic input hiding,
reject literal web credentials, preserve unresolved markers in public export,
and include only HMAC/fingerprint derivatives in configuration identity.

Plaintext credentials must never enter Landscape data, source or transform
rows, success reasons, telemetry, logs, exception text, validation text,
catalog metadata, or exported configuration. The transform declares
`AuditCharacteristic.CREDENTIALS`.

## Transform architecture

### `aws_textract_inline_analysis`

Required plugin metadata:

- `name = "aws_textract_inline_analysis"`
- `determinism = Determinism.EXTERNAL_CALL`
- `plugin_version = "1.0.0"`
- `passes_through_input = True`
- `creates_tokens = False`
- `audit_characteristics = {AuditCharacteristic.CREDENTIALS}`
- capability tags covering AWS, Textract, document analysis, OCR, inline,
  blob, and enrichment

The class implements `BaseTransform` plus `BatchTransformMixin`. Ordinary
`process()` is unavailable; the engine uses `connect_output()` and `accept()`
so documents may execute concurrently while output remains FIFO.

### Audited synchronous client

A focused client adapter wraps one shared, thread-safe boto3 Textract client.
It owns:

- exact `AnalyzeDocument` request construction with `Document.Bytes`;
- one Landscape call record per logical SDK invocation;
- rate-limiter acquisition before the invocation;
- semantic response-size measurement;
- SDK error classification and sanitization; and
- safe telemetry only after successful audit persistence.

The boto3 client uses botocore standard retries with three total attempts, a
10-second connect timeout, and the configured read timeout. The transform adds
no second retry loop. `on_start()` creates the SDK client; shutdown closes it
once. Each row gets a small audited wrapper bound to its `state_id` and
`token_id`, without sharing audit parentage.

### Shared pure result parser

The asynchronous and inline transforms use one pure Textract result parser.
It imports no boto3, Landscape, executor, or lifecycle types. It owns block
indexing, duplicate-ID detection, relationship resolution, page membership,
ordering, text assembly, table reconstruction, form reconstruction,
query/result matching, signatures, layout, geometry validation, page-count
validation, model metadata, and provider-shaped aggregate construction.

The asynchronous adapter supplies its combined paginated semantic response;
the inline adapter supplies the one semantic `AnalyzeDocument` response. Known
members are strictly typed. Unknown members inside valid blocks are preserved
only when the optional native result is enabled. Unknown top-level members are
omitted because their stable output meaning is undefined. A malformed known
member or dangling relationship fails the row closed. Because V1 never sends
`HumanLoopConfig`, an unexpected `HumanLoopActivationOutput` is a malformed
known response rather than a field to discard silently.

## Per-row data flow

1. Require Landscape, run identity, row state identity, token identity, rate
   limiter, payload store, and initialized SDK client.
2. Read `blob_ref_field` and require a 64-character lowercase SHA-256 value.
3. Retrieve the document from `PayloadStore`; integrity mismatch propagates as
   an infrastructure failure.
4. Enforce non-empty content and `max_document_bytes`.
5. Verify the exact configured format signature.
6. Construct `AnalyzeDocument` with `Document.Bytes`, canonical feature types,
   and optional single-page queries.
7. Acquire rate-limit capacity and call the audited client once.
8. Remove SDK `ResponseMetadata`, enforce semantic byte and block limits, and
   validate the complete response graph.
9. Require `DocumentMetadata.Pages == 1` and exactly one page block numbered
   `1`.
10. Build only configured projections and the optional native response.
11. Propagate and align the input/output schema contract.
12. Emit exactly one enriched row or one row-level error.

The transform never creates a barrier across all input rows. Each worker owns
one retrieval/call/parse sequence; `BatchTransformMixin` supplies backpressure
and FIFO release.

## Landscape call contract

Every `AnalyzeDocument` invocation records one call with `CallType.HTTP` and
provider `aws_textract`.

### Request audit data

The semantic audited request contains:

- operation `analyze_document`;
- region;
- source payload hash and byte length;
- declared document format;
- canonical feature types;
- query configuration; and
- no document bytes, base64, credentials, authorization headers, or AWS
  signing material.

The payload hash is sufficient to bind the call to the integrity-checked source
content already held by ELSPETH. Duplicating bytes into call audit would add a
second retention surface without improving reproducibility.

### Successful response audit data

The audited response stores the bounded semantic provider response after
removing SDK `ResponseMetadata`. It includes document metadata, model version,
and blocks. Landscape's existing content-addressed payload storage handles the
call data; the transform creates no parallel blob store.

The optional row `result_field` may repeat the bounded semantic result in row
state. That retention cost is explicit operator opt-in and is why the field is
disabled by default.

### Failure audit data

Failure records contain only:

- ELSPETH-owned error category;
- bounded AWS error code;
- retryable flag;
- SDK attempt count when available; and
- operation name.

Raw exception strings, provider status messages, response headers, document
content, and credentials are never copied into errors or logs. Telemetry is
emitted only after audit persistence and contains stable hashes and bounded
summaries, never blocks or bytes.

## Normalized output contract

The shapes and ordering rules are identical to
`aws_textract_document_analysis` so downstream nodes can switch between S3
asynchronous and blob synchronous ingestion without changing projection
logic.

### Text and pages

`text_field` joins validated `LINE.Text` values in provider arrival order with
newlines. `extract.pages` is a one-item list containing page number, text,
geometry, and ordered line objects. Geometry retains validated Textract
bounding boxes and polygons.

### Tables, forms, queries, signatures, and layout

- Tables preserve one-based row/column coordinates, spans, sparse positions,
  selection state, confidence, and geometry.
- Forms preserve key/value block identities, text, confidence, and geometry;
  a valid key without a value emits null value members.
- Queries preserve question, alias, answer, confidence, and block identities;
  an unanswered query emits null answer members.
- Signatures preserve block ID, page, confidence, and geometry.
- Layout preserves exact layout block type, child-derived text, confidence,
  geometry, and provider order.

### Metadata

`metadata_field` contains:

```json
{
  "document_sha256": "64-lowercase-hex-content-hash",
  "document_size_bytes": 183421,
  "document_format": "png",
  "page_count": 1,
  "block_count": 412,
  "model_version": "provider-model-version",
  "feature_types": ["FORMS", "TABLES"]
}
```

### Provider-shaped result

`result_field` contains:

```json
{
  "DocumentMetadata": {"Pages": 1},
  "AnalyzeDocumentModelVersion": "provider-model-version",
  "Blocks": []
}
```

It excludes `ResponseMetadata` and unknown top-level members. A response that
contains `HumanLoopActivationOutput` fails validation because human-loop
configuration is unsupported. Unknown members inside individual blocks remain
available when the aggregate fits the configured byte bound.

## Error and retry contract

### Local non-retryable row errors

- missing or non-string input field;
- malformed payload hash;
- payload not found;
- empty document;
- document over the configured size bound;
- configured-format signature mismatch;
- provider-declared bad, unsupported, excessive, or invalid document;
- returned page count other than one;
- malformed or unbounded semantic response; and
- invalid known block member, duplicate block ID, or dangling relationship.

### Retryable row errors

After SDK retry exhaustion, throttling, provisioned-throughput, transient
service, connection, and timeout failures are retryable. Authorization and
configuration failures are not.

### Infrastructure failures

Payload integrity corruption, missing lifecycle services, absent audit
identity, and impossible SDK/config invariants propagate as framework failures
rather than being disguised as provider row failures.

Synchronous `AnalyzeDocument` has no idempotency token. Botocore may repeat an
attempt according to its standard retry policy, and a later executor-level row
retry may create another billable provider invocation. Plugin help, success
metadata, and operator documentation must state this limitation. V1 does not
introduce a cross-row result cache because that would change audit parentage,
provider freshness, and billing semantics.

## Schema and configuration identity

`blob_rows` declares its five fixed output fields and a homogeneous row
contract. The Textract transform declares its configured input and output
fields, passes through upstream fields, narrows the input contract to observed
output data, applies declared field guarantees, and aligns the output schema
through existing infrastructure.

The transform's configuration identity includes, through the standard safe
configuration fingerprint:

- plugin and implementation versions;
- region and authentication mode;
- credential fingerprints, never plaintext;
- blob input field;
- declared document format;
- canonical feature types and ordered queries;
- output mapping; and
- all input/output/time/size bounds that affect behavior.

Source configuration identity includes the ordered authoritative blob IDs,
payload hashes, bounded metadata, source plugin version, and schema policy.

## Composer behavior

Composer assistance and tool descriptions must state:

- pasted binary documents become managed blobs, not inline base64;
- one blob produces one source row;
- multiple blobs preserve their authoring order as multiple rows;
- `document_format` is required and runtime never infers it;
- Composer may choose the format, but the saved configuration makes that
  interpretation reviewable;
- mixed formats require homogeneous named sources/branches and separate
  transform instances;
- runtime signature mismatch fails closed;
- JPEG is spelled `jpeg` in configuration;
- PDF support is single-page only;
- multipage, large, or already-S3 documents should use
  `aws_textract_document_analysis`; and
- synchronous retries can repeat billable calls.

The Composer must not silently convert, downsample, merge, or re-encode a
document to make it fit. Such a transformation would create different source
bytes and requires an explicit future plugin.

## Implementation boundaries

The implementation should produce:

1. a shared binary-document signature helper below the web/plugin layers;
2. storage-level MIME vocabulary and persistence alignment without widening
   text-only tools;
3. authenticated binary paste/upload admission;
4. authoritative plural blob-source binding and run-admission validation;
5. the generic `blob_rows` source;
6. narrow shared Textract AWS/authentication/error utilities;
7. the shared pure result parser, if it is not already extracted by the
   asynchronous implementation; and
8. the `aws_textract_inline_analysis` transform and its discovery/help
   surfaces.

The synchronous implementation must not import asynchronous orchestration.
Only pure parsing and narrowly defined AWS helpers are shared. Neither
transform grows an `execution_mode` setting.

## Verification strategy

### Binary blob and source tests

- storage MIME closed-set and database/API alignment;
- text `create_blob`/`update_blob` rejection of binary MIME values;
- valid JPEG, PNG, and PDF upload/paste admission;
- empty, truncated, mismatched, oversized, and unsupported binary rejection;
- idempotent binary blob creation and full provenance;
- plural binding of one and several blobs;
- duplicate, foreign, missing, non-ready, changed, and mixed-authority failure;
- no partial composition mutation when one list member fails;
- run admission revalidation and input linking for every blob;
- source ordering and exact five-field rows;
- proof that source rows and tool audit never contain bytes, base64, or storage
  paths; and
- retention/deletion protection for every referenced blob.

### Transform configuration tests

- required region, explicit format, feature vocabulary, and output target;
- default-chain and secret-reference cross-field rules;
- literal-secret rejection and safe public export;
- output-field uniqueness and schema declarations;
- feature/query coupling and the 15-query ceiling;
- single-page query selector restrictions;
- hard 5 MiB maximum and positive result/time limits; and
- effective batch wait covering SDK retry/timeout headroom.

### Transform behavior tests

- valid JPEG, PNG, and PDF signatures;
- signature mismatch, embedded-later signature, leading bytes, empty input,
  missing blob, and oversized blob;
- integrity corruption propagation;
- exact boto3 `Document.Bytes`, features, and queries construction;
- proof that original bytes are passed to boto3 without row-level base64;
- rate-limiter use, SDK config, client lifecycle, and shutdown;
- one-page success plus zero/multiple-page rejection;
- every normalized projection and native-result bound through the shared
  parser corpus;
- malformed graph, geometry, page, block-count, and byte-size failures;
- sanitized AWS error categories and retryability;
- one audited call per row with correct state/token parentage;
- proof that document bytes, base64, credentials, and provider messages never
  enter audit metadata, errors, telemetry, or output rows;
- concurrent rows, backpressure, timeout, and FIFO release; and
- forward invariant and contract propagation coverage.

### Composer and integration tests

- one pasted image -> one blob -> one source row -> one analysis;
- several same-format images -> ordered rows and analyses;
- mixed formats -> homogeneous sources/branches and explicit transform
  settings;
- Composer-authored format preserved through export/import;
- Composer/runtime disagreement rejected at byte-signature validation;
- web deferred-secret resolution for explicit AWS credentials;
- trusted CLI YAML with pre-existing payload refs, plus a pipeline path where
  an upstream component emits the payload hash;
- opt-in live AWS JPEG, PNG, and single-page PDF acceptance; and
- discovery, catalog, examples, and assistance metadata.

Before handoff, run the full `pytest tests/` suite, `elspeth-lints check`, and
`wardline scan . --fail-on ERROR`. The live inline-PDF acceptance test is a
release gate because AWS's general and operation-specific documentation does
not align perfectly with the generic `Document.Bytes` reference.

## Acceptance criteria

The design is complete when:

1. authenticated paste/upload can store JPEG, PNG, and PDF bytes under normal
   blob custody without widening string-based blob tools;
2. Composer can bind one or more such blobs to `blob_rows` atomically;
3. `blob_rows` emits one authoritative content-hash row per blob with no bytes;
4. Composer persists an explicit homogeneous `document_format` choice;
5. the transform retrieves, verifies, and sends exactly those bytes to
   synchronous `AnalyzeDocument`;
6. every successful response proves exactly one page and passes the shared
   strict parser;
7. outputs match the asynchronous plugin's normalized contracts;
8. credentials use the same secret infrastructure as other plugins;
9. audit evidence is reproducible through content hashes without duplicating
   document content; and
10. all local gates and live inline-PDF acceptance pass.

## References

- [AnalyzeDocument API](https://docs.aws.amazon.com/textract/latest/APIReference/API_AnalyzeDocument.html)
- [Textract Document input](https://docs.aws.amazon.com/textract/latest/APIReference/API_Document.html)
- [Processing documents synchronously](https://docs.aws.amazon.com/textract/latest/dg/sync.html)
- [Calling synchronous operations](https://docs.aws.amazon.com/textract/latest/dg/sync-calling.html)
- [Textract Queries FAQ](https://aws.amazon.com/textract/faqs/)
- [Asynchronous Textract transform design](2026-07-29-amazon-textract-document-analysis-transform-design.md)
