# Azure document analysis: Document Intelligence fixes and a Content Understanding transform

Date: 2026-11-11. Status: implemented on `claude/vibrant-bardeen-ylh2hw`.

## Intent

1. Audit `azure_document_intelligence` for bugs, gaps and enhancement
   opportunities, and fix what the audit finds.
2. Add `azure_content_understanding`, a transform for Azure AI Content
   Understanding that works the same way: one document reference per row, an
   asynchronous analyze operation, and the result written onto the row.

Done means both plugins are tested and ready to merge.

## API facts this design relies on

These were checked against the `azure-rest-api-specs` spec JSON
(`specification/ai/data-plane/...`) and Microsoft Learn on 2026-11-11.

| | Document Intelligence | Content Understanding |
|---|---|---|
| GA api-version | `2024-11-30` (still the latest) | `2025-11-01` |
| Submit | `POST /documentintelligence/documentModels/{model}:analyze` | `POST /contentunderstanding/analyzers/{analyzer}:analyze` |
| URL body | `{"urlSource": url}` | `{"inputs": [{"url": url}]}` |
| Inline body | `{"base64Source": b64}` | `{"inputs": [{"data": b64, "mimeType"?: ...}]}` |
| Poll | `Operation-Location` → `.../analyzeResults/{id}` | `Operation-Location` → `.../analyzerResults/{id}` |
| Status values | `notStarted running succeeded failed canceled skipped` | `NotStarted Running Succeeded Failed Canceled` |
| Result | `analyzeResult` (`content`, `pages`, `documents[].fields`, ...) | `result` (`contents[]`, each `markdown`, `fields`, `pages`, ...) |
| Field value | `{"type": T, "value<T>": v, "confidence", ...}` | same shape, smaller type set |
| Errors | `{"error": {"code", "message", "innererror": {"code"}}}` | same |

Not verified: the CU migration guide says inline uploads use `:analyzeBinary`,
while the GA spec still defines `inputs[].data` on `:analyze`. The
transform follows the GA spec. The live test exercises URL mode. Also not
verified: the complete list of CU endpoint host suffixes; the transform
admits the two hosts used in the official examples.

## Document Intelligence audit

| # | Finding | Kind | Fix |
|---|---|---|---|
| 1 | A SAS `sig` in a `urlSource` value was written raw into the Landscape request record, although the poll URL and api-key header were already fingerprinted. | Security bug | The audit records `fingerprint_url(url)` via `audit_json`. The wire body still carries the URL verbatim. |
| 2 | The documented `canceled` and `skipped` statuses were reported as `malformed_response/unknown_status`. | Bug | Both now report `analysis_failed`, with `error_type` (`failed`/`canceled`) and a bounded `cause`. |
| 3 | The batch-mixin wait budget left out `request_timeout_seconds`, so a row with a long request timeout could outlive the wait and be cancelled. | Bug | The budget is now `poll + capacity + request_timeout + 30s`. |
| 4 | A rejected submit or poll call reported only the HTTP status. | Gap | Bounded `code` (`error.code`, as Textract reports provider codes) and `cause` (`innererror.code`) tokens are taken from Azure's error envelope; no new `TransformErrorReason` key. `error.message` is never read. |
| 5 | 429/503 retries ignored `Retry-After`. The backoff restarted at 50 ms and was capped at 1 s, so one row could make about 300 billed, audited calls. | Gap | `Retry-After` lengthens the wait. The per-row budget still bounds it. |
| 6 | A running poll's `Retry-After` was ignored. | Gap | It is honoured, bounded by the poll deadline. |
| 7 | Invalid base64 (including a `data:` URI prefix) was sent to Azure, a billed and audited call that Azure then rejects. | Gap | Strict local validation produces `invalid_input/invalid_base64`. |
| 8 | There was no plain-value view of typed fields (prebuilt-invoice, custom models, `queryFields`), so a pipeline had to dig through `documents[0].fields[*].valueX`. | Enhancement | New option `fields_field`: `{name: value}` for the first document. |
| 9 | Analysis metadata reached `success_reason` only, never the row. The Textract transforms already have `metadata_field`. | Enhancement | New option `metadata_field`, with bounded metadata and warning codes. |
| 10 | The service-reported `modelId` was echoed into metadata without any bound. | Hardening | It is echoed only when it matches the model-id grammar. |

Considered and left out:
- Entra ID / managed-identity auth. The sibling Azure transforms are key-only,
  and it needs token refresh and an `azure-identity` seam. The auth header
  method stays isolated so it can be added cleanly later.
- Treating 500/502/504 as retryable. That would diverge from the sibling Azure
  transforms, and a resubmit is a fresh billed analysis.
- An audit digest instead of the full base64 body. That would weaken audit
  evidence, and the current behaviour is documented.

## Content Understanding transform

### Shape

`azure_content_understanding` is a sibling of `azure_document_intelligence`.
Both are subclasses of a shared core:

- `analyze_lro.py` holds `AzureAnalyzeConfig` and `AzureAnalyzeTransform`.
  This is the shared lifecycle, client cache, submit, poll, capacity retry,
  audit projection, error vocabulary and ADR-009 probe. It contains
  everything in the table above that is not service-specific.
- `analyze_result.py` holds the shared Tier-3 parsers: host pinning, operation
  id, error envelope, warning codes, list facets and typed field flattening.
- Each service module supplies the analyze URL, the request bodies, the
  status map and the result-to-row projection.

Alternatives considered: copying the Document Intelligence module (two copies
of about 600 lines, so every fix lands twice), or one transform with a
`service:` switch (two unrelated configuration surfaces in one plugin).
The shared base keeps each service module small and puts the security
behaviour in one place.

### Configuration

Shared with Document Intelligence: `api_key`, `source_mode` (`url|base64`),
`source_field`, `max_base64_chars`, the poll, timeout and retry knobs, and the
schema config.

Content Understanding specific:

- `endpoint`: HTTPS origin on `*.services.ai.azure.com` or
  `*.cognitiveservices.azure.com`, port 443, no path.
- `api_version`: `2025-11-01` only.
- `analyzer_id`: `^[a-zA-Z0-9._-]{1,64}$` (prebuilt or custom).
- `content_range`: per-input `range` (`1-3,5,9-`; milliseconds for
  audio/video).
- `mime_type`: optional `inputs[].mimeType` for base64 inputs.
- `processing_location`: `geography|dataZone|global` (query parameter).
- `string_encoding`: `codePoint|utf16|utf8` (query parameter).
- `model_deployments`: `{model: deployment}` request override.

Outputs (at least one is required, and names must be unique):

- `content_field`: `str`, the markdown of every content joined by a blank line.
- `fields_field`: flattened `fields` of the first content.
- `page_count_field`: `int`, the sum of `endPageNumber - startPageNumber + 1`
  over document contents.
- `metadata_field`: analyzer id, api version, operation id, content count,
  page count and warning codes.
- `result_field`: the whole `result`.
- `extract.{contents,pages,tables,paragraphs,sections,figures,hyperlinks,transcript_phrases}`:
  list facets, concatenated across contents.

### Failure vocabulary (both transforms)

`missing_field`, `non_string_field`, `invalid_input` (`invalid_document_url`,
`base64_too_large`, `invalid_base64`), `api_error` (`submit_rejected`,
`poll_request_failed`, plus `code`/`cause`),
`operation_location_missing`, `operation_location_untrusted`,
`analysis_failed` (`error_type` = `failed`/`canceled`, `cause`), `malformed_response`, `poll_timeout`,
`retry_timeout`, `body_too_large`, `shutdown_requested`. A network error
raises `PluginRetryableError`. Every failure is per-row and non-retryable.

## Testing

- Unit tests drive both transforms through a scripted fake client: the happy
  path, every failure reason, Retry-After, budgets, base64 validation, the
  audit projection, and field flattening.
- A respx test drives the real `AuditedHTTPClient` and proves the wire body
  carries the SAS while the recorded call does not.
- The shared parsers have their own unit tests.
- ADR-009 forward-invariant contract tests cover both transforms.
- A protected live PB-09 lifecycle test is added for Content Understanding.
  It skips when unconfigured.
- Mutation checks were applied to each Document Intelligence fix, and every
  mutation was caught.
- The whole-tree gates in CONTRIBUTING's new-plugin checklist are updated:
  discovery counts, catalog, knob-schema golden, state-engine matrix,
  allowlists and source hashes.

## Gate outcomes

- **Trust tier.** Every Tier-3 reader is a declared `@trust_boundary` with a
  cited test, or an `@observation_boundary` where it never raises:
  `parse_poll_body`, the field flatteners, `warning_codes`, `facet_list`,
  `parse_result_contents`, the DI result parsers, and the bounded
  token/id/error-code readers. Per-row failures leave the submit/poll helpers
  as `_RowFailed` rather than as `TransformResult` unions. Raw R5 hits under
  `plugins/transforms/azure/*` fell from 16 (at the ceiling) to 3. The keyless
  corpus adds no findings beyond `R_TB_SUPPRESSED` observations.
- **Soft-mapping census.** +18 soft sites over the base when first measured (2,617 → 2,635; the
  pin is 2,668 after the rebase onto 0.8.13), with 5 more boundary-parsed. The new sites are the provider JSON
  surfaces of a second service. Result contents parse once into
  `ResultContents`, and the emitted metadata is an owned TypedDict.
- **Planner scaffolding.** The new catalogue entry adds a measured 595 B to the
  fixed planner request. That is the entry alone: without it the request is
  140,468 B after the rebase onto 0.8.13 (7 B under the prior ceiling), and
  the DI entry is byte-identical. It is
  priced as `_CONTENT_UNDERSTANDING_CATALOG_CANONICAL_BYTES`, and the
  maintainer should review it like the other allowances there.

## Compatibility

Document Intelligence option names and defaults are unchanged; there are two
new optional outputs. Behaviour changes:

- `canceled`/`skipped` report `analysis_failed` instead of `malformed_response`.
  A failed operation with no error code still reports `cause: unknown`.
- Base64 that is not padded standard-alphabet base64 is rejected locally. That
  includes line-wrapped or whitespace-containing input, which was previously
  forwarded to Azure.
- `max_capacity_retry_seconds` bounds each call's 429/503 retrying, clamped to
  the poll deadline, instead of being measured from the row start. A capacity
  `Retry-After` longer than the remaining budget fails the row at once. A poll
  `Retry-After` is honoured up to 60 seconds.
- A `malformed_response` message names the facet without the `analyzeResult.`
  prefix, and `operation_id` is omitted when Azure's id is not a plain token.
- The catalogue lists the shared source, polling and timeout options before
  `endpoint` and `model_id`, because they now come from the shared config base.
- The plugin's `source_file_hash` and lifecycle owners move, because the
  shared machinery moved into `analyze_lro`.

## Review follow-ups (2026-11-11)

An independent review of the first commit found these, all fixed with tests:

- Both plugins had dropped out of the source-hash gate, because the rule only
  recognises direct plugin bases. `AzureAnalyzeTransform` is now a recognised
  base in the lint rule and in `scripts/cicd/plugin_hash.py`.
- Two exceptions could still end the run: `urlparse` raising on a malformed
  bracketed host in the row value, and the HTTP client's encoding-limit error.
- The capacity budget was anchored to the row start, so a 429 on a late poll
  failed with no retry. An oversized `Retry-After` slept the whole budget and
  then failed anyway.
- The base64 check held about 255 MB of backtracking state for an 8M-character
  input. The pattern is now possessive.
- `ResultContents` froze the result and thawed it once per output. It now holds
  the parsed contents as-is.

Known limits, not changed here:

- A plugin's `source_file_hash` covers its own file. The shared `analyze_lro`
  and `analyze_result` modules are not in any plugin hash, as was already true
  of `document_intelligence_result.py`.
- The SAS fingerprinting covers the recorded HTTP request. The row's own
  `source_field` value is stored unchanged wherever row data is stored, and the
  fingerprinting recognises credentials by query-parameter name.
- Azure's error response bodies are recorded by the HTTP client as received.
- The Content Understanding endpoint allowlist has no sovereign-cloud hosts.
- The live-provider workflow file needs the four-line change described in the
  hand-off, because the agent token cannot push workflow files.
