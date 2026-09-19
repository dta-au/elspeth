# Azure Document Intelligence capability expansion

## Purpose and boundary

`azure_document_intelligence` currently enriches one pipeline row from one Azure Document Intelligence analysis. This spec describes a separate body of work after the September 2026 polling fixes. It aims to make the 2024-11-30 service's document, authentication, and generated-output capabilities usable without weakening per-row audit, routing, or reproducibility.

The current transform already accepts a document URL or base64 value, a model ID, pages, locale, string index type, analysis features, query fields, and text or Markdown content. It can emit content, page count, selected top-level facets, and the complete `analyzeResult`. It uses an API key and records an audited submit plus each poll. The `figures` facet is metadata; it is not a cropped image.

Azure also offers a [binary stream submit](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/analyze-document-from-stream?view=rest-aiservices-v4.0+(2024-11-30)), [`output=pdf` and `output=figures`](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/analyze-document?view=rest-aiservices-v4.0+(2024-11-30)), separate [PDF](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/get-analyze-result-pdf?view=rest-aiservices-v4.0+(2024-11-30)) and [figure](https://learn.microsoft.com/en-us/rest/api/aiservices/document-models/get-analyze-result-figure?view=rest-aiservices-v4.0+(2024-11-30)) retrieval endpoints, and OAuth2 authentication. Those are not currently exposed by this transform. The existing `document` sink accepts text, so it cannot publish these binary outputs as-is.

## Required behavior

### 1. Admit documents as bytes without converting them to base64 row strings

Add an explicit binary input mode for an owned document value or artifact reference. Keep `url` and `base64` modes for existing YAML. The new mode must accept only documented MIME types and a size-bounded byte stream. Validate declared MIME against the byte signature where ELSPETH has a signature contract (`src/elspeth/contracts/binary_documents.py`); reject unsupported or mismatched documents before the Azure call. The request uses Azure's stream endpoint and a bounded audited payload representation, not an unbounded copy of document bytes in a row or log. Its provenance must identify the exact input bytes by digest and length. No implicit conversion from an arbitrary string or URL is allowed.

The binary mode depends on a reusable audited HTTP binary request contract. Do not add an unaudited direct `httpx` call inside this plugin. The contract must record status, size, digest, content type, elapsed time, and error classification while respecting the existing body cap and credential scrubbing rules. Keep raw bytes in the configured artifact store under its retention policy.

### 2. Publish generated PDF and cropped figures as artifacts

Add an optional generated-output declaration with `pdf` and `figures` choices. Send only the selected values in Azure's `output` query parameter. Once analysis succeeds, retrieve exactly the requested outputs through the audited client. Validate each response's media type, size, and signature (`application/pdf` for PDF, `image/png` for figures); fail the row if a requested output is missing, malformed, truncated, or cannot be published. A successful JSON result alone is not success when binary outputs were requested.

Persist bytes through a recoverable, idempotent artifact effect. The row receives owned artifact references containing the logical output kind, MIME type, byte length, digest, and durable artifact identity; it never receives a raw byte array or a local temporary path. Figure references also carry the Azure figure ID and preserve the order of the `analyzeResult.figures` list. Validate figure IDs from Azure's result before constructing retrieval paths. Audit links each retrieval call and publication effect to the source row and operation, and a retry must neither duplicate an artifact nor silently replace different bytes under one identity.

The binary publication contract may be implemented in the existing artifact/effect infrastructure or a new narrow extension to it. Its design must be settled before adding `output` to the plugin config: the current text-only `DocumentSink` is not an acceptable substitute. Enforce an explicit per-artifact byte cap and a total per-row cap, including all figures.

### 3. Support Microsoft Entra authentication

Make authentication an explicit union: API key, managed identity, or service principal. API-key YAML remains valid. Entra modes acquire tokens for the Cognitive Services scope documented by Azure and send bearer headers for submit, poll, and artifact retrieval. Keep the selected auth method stable for an operation; refresh expiring tokens without exposing them to row data, call payloads, exception text, or telemetry. Reject mixed or incomplete credentials at config validation. Storage-account connection strings and SAS modes in `azure_auth.py` are storage-specific and must not be copied into this service config.

### 4. Preserve the row contract and failure semantics

For every mode, submit once per attempted analysis, poll under one deadline, and classify network/capacity failures for the existing retry path. Honor `Retry-After` without a hot loop. A retry following a completed Azure analysis needs an explicit idempotency decision: either safely resume the recorded operation and its artifact retrievals, or clearly record and budget a new analysis. Do not infer completion from a partial local artifact set. Keep all generated metadata in `success_reason["metadata"]`; only declared extraction fields and artifact references enter the output row.

Do not silently synthesize empty requested content or facets from a malformed successful response. Distinguish an absent optional facet from an explicitly requested output that Azure failed to produce. Keep raw provider error messages and credential-bearing URLs out of routable error reasons.

## Implementation sequence

1. **Freeze the contracts.** Define binary input and artifact-reference DTOs, size limits, MIME/signature rules, and the recoverable publication lifecycle. Review the exact affected files in `src/elspeth/contracts/`, `src/elspeth/plugins/infrastructure/clients/http.py`, `src/elspeth/core/landscape/execution/artifacts.py`, and the sink-effect coordinator. Prove that retries and crashes cannot create a row that points to an absent artifact.
2. **Implement and test the audited binary transport.** Add stream submit and bounded binary response handling to the shared HTTP client, with focused tests for oversize bodies, response interruption, redirects, error recording, and credential-safe payloads. This is a dependency of both binary input and generated output.
3. **Add binary input.** Extend `AzureDocumentIntelligenceConfig` and `_submit` in `src/elspeth/plugins/transforms/azure/document_intelligence.py`; test URL, base64, and binary modes through the same analysis path. Preserve the existing default schema and YAML behavior.
4. **Add generated artifacts.** Extend the config and result parsing, then implement PDF and figure retrieval/publication. Test one PDF, multiple figures, empty figure list, missing requested output, invalid figure ID, wrong MIME/signature, size cap, retry, and crash reconciliation. Verify that the JSON `figures` facet still means metadata.
5. **Add Entra auth.** Introduce the config union and credential adapter, then test token acquisition/refresh, all request legs, mixed-auth rejection, and secret redaction. Cover managed identity and service principal in a protected Azure environment.
6. **Update authoring surfaces and examples.** Update the catalog knob schema, plugin assistance, user manual, environment-variable reference, and a maintained example that demonstrates text extraction plus artifact references. Recompute `source_file_hash` after formatting and run the affected catalog, contract, source-hash, and DAG gates.

Each step is independently reviewable. No compatibility alias or database migration is required. If an artifact schema must change, recreate the pre-release test and local databases under the project's delete-and-recreate policy; do not carry a migration path.

## Acceptance

- Focused unit tests prove config discrimination, MIME/signature admission, result parsing, poll and retry deadlines, and artifact failure routing. Mocked HTTP integration tests prove the complete submit → poll → retrieve → publish lifecycle, including crash/retry idempotency and audited call order.
- Regional integration runs use the protected Azure Document Intelligence lane with operator-approved endpoint, model, document, and credentials. Exercise both API-key and Entra modes, and at least one generated PDF and figure where the model supports them. A skipped test caused by missing regional settings is not acceptance evidence.
- The full release gate runs at integration time, including static analysis, contract/source-hash gates, and relevant PostgreSQL testcontainer tests if artifact persistence changes. No live credential is placed in a tracked file or test log.
- The old URL/base64 pipeline examples and their output fields remain valid. New binary outputs are durable, content-addressed, and discoverable from the Landscape audit trail; a requested but unpublished artifact makes the row fail closed.

## Exclusions

Azure's batch-analysis endpoint is a separate workflow: this plugin's row-level calls and audit parentage should not be silently replaced by a service batch. Model administration, model training, and document review judgments are also separate products. This transform extracts evidence; it does not decide whether a document passes review.
