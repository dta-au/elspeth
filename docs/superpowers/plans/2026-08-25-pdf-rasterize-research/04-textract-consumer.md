# Research lane 4 — `aws_textract_inline_analysis` as the downstream consumer

Read-only sweep at `a371e13d0`, 2026-08-25.

## Headline

The input contract is exactly ONE row field: a 64-lowercase-hex payload ref under the
field named by `blob_ref_field` (default `blob_ref`), whose bytes start with the
signature of the configured `document_format` (`png`). No executable change is required
in `textract_inline_analysis.py` — only prose.

## 1. Input contract

`_read_document_bytes` (`textract_inline_analysis.py:452-508`):
`_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")` (`:68`); `type(x) is not str`
exact-type check (`:463`). Config option `blob_ref_field` (`:123-128`) is the ONLY
row-field option; `declared_input_fields` (`:240-242`). Failure order: missing_field →
invalid_input/actual_type → invalid_input/invalid_payload_ref → blob_not_found →
invalid_input/empty_document → blob_too_large → invalid_input/document_signature_mismatch.
`IntegrityError` propagates (`:453-458`).

`max_document_bytes` (`:158-163`): `default=BINARY_DOCUMENT_MAX_BYTES, gt=0,
le=BINARY_DOCUMENT_MAX_BYTES`; re-checked `:488-497`. A 300-DPI A4 PNG is routinely
2–8 MiB → `pdf_rasterize` must own the per-page byte bound.

`document_format` is CONFIG (`:129-131`), `Literal["jpeg","png","pdf"]`; runtime checks
agreement via `binary_document_signature_matches` (`:498`); PNG magic
`b"\x89PNG\r\n\x1a\n"`. No MIME field, no sniffing.

`blob_rows` emits five fields (`blob_rows.py:224-232`); textract reads only `blob_ref`.
Minimum honest row for pdf_rasterize output: the page ref field; `document_id` and the
page number ride through as pass-through payload.

## 2. Nothing textract needs is unproducible

No URI/filename/MIME/size read. Contrast `aws_textract_document_analysis` which needs
`bucket_field`/`key_field` (`textract_document_analysis.py:1083-1085`) — unusable
downstream of a rasterizer. Recommend NOT reusing `blob_*` names for page metadata
(they carry blob_rows' web-custody semantics); use `page_*`.

## 3. `Pages != 1` guard (`:556-565`) — stays

Validates the provider RESPONSE. Test already exists:
`tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py:646-656`
(`test_multi_page_response_fails_page_count_policy`, `page_count=2`, config default
`document_format: "png"`). No new mechanism → add a provenance comment naming the
rasterize case rather than a duplicate test. Query page selector restriction `:80-85`
(`["1"]`/`["*"]`) stays correct: each rasterized page is page 1 of its own PNG.

## 4. Prose surfaces — current text and gates

`usage_when_to_use` `:266-270`:
> "Use when each row carries a payload-store content hash (from the blob_rows source) for a JPEG, PNG, or single-page PDF document up to 5 MiB and you need synchronous Textract OCR, forms, tables, queries, signatures, or layout enrichment. Extracted remote content remains untrusted before LLM consumption."

`usage_when_not_to_use` `:271-275`:
> "Not for multipage or larger documents, or documents already stored in S3 — use aws_textract_document_analysis for those. AnalyzeDocument has no idempotency guarantee, so SDK and engine retries can each repeat a billable provider call."

`composer_hints` `:723-732` (six strings; the tuple opens at :723):
1. "Rows come from the blob_rows source; the default blob_ref_field matches its blob_ref output."
2. "document_format is required and never inferred — spell JPEG as jpeg; the runtime rejects a byte-signature mismatch fail-closed."
3. "PDF support is single-page only; mixed formats need homogeneous sources or branches with one transform instance per format."
4. "Multipage, oversized, or already-S3 documents belong in aws_textract_document_analysis."
5. "Synchronous AnalyzeDocument has no idempotency guarantee, so SDK and engine retries can each repeat a billable provider call."
6. "For explicit AWS credentials, use ELSPETH markers such as {secret_ref: AWS_ACCESS_KEY_ID}."

Gates:
- A `test_external_catalogue_metadata.py:102-105`: to-use MUST contain (casefolded)
  `synchronous, blob_rows, payload-store, ocr, single-page, 5 mib, untrusted before llm`;
  avoid MUST contain `multipage, s3, billable`. **Keep every substring; edit no gate.**
  Honest resolution: each rasterized page is page 1 of its own single-page PNG, so the
  single-page contract is unbroken; the rewrite only ADDS "or one rasterized page per row
  from pdf_rasterize" and "a multipage PDF must be rasterized to per-page images first
  (pdf_rasterize) or analyzed with aws_textract_document_analysis".
- B `test_catalog_reference_content.py:294-316` global uniqueness.
- C `test_external_catalogue_metadata.py:273-280` placeholder scan (`todo, tbd,
  replace-me, placeholder, see the technical description`).
- D `tests/fixtures/catalog_reference.py:75-85` per-plugin distinctness.
- E `test_textract_inline_analysis.py:814-824`: hints must contain `blob_rows`, `jpeg`,
  `single-page`, `aws_textract_document_analysis`, `billable`. Additive edit is safe.
- F `capability_tags` `:263-264` is 6/6 and two-file exact pinned
  (`test_external_catalogue_metadata.py:43,:261`) — change nothing.

`source_file_hash` `:258` = `"sha256:7beadca2550f4b0f"` (verified exact at HEAD). The
`plugins/transforms/aws` directory is NOT in the lint's `PLUGIN_DIRS` (`rule.py:23-31`,
non-recursive glob) — its hash is UNGATED; keep it correct by discipline. `blob_rows.py`
(`:124`, `sha256:083891225d454848`) IS gated. Recompute: finish edits → `ruff format`
→ `compute_source_file_hash` → paste; strict equality.

Other prose to update (all additive, none golden-pinned):
- `textract_document_analysis.py:324-328` `usage_when_not_to_use` ("…up to 5 MiB
  (single-page for PDF), use aws_textract_inline_analysis…"; gate needs `inline bytes`,
  `synchronous` in avoid); `:1086` composer hint.
- `blob_rows.py:7` module docstring; `:171` composer hint ("Mixed document formats need
  homogeneous sources or branches — one explicitly configured consumer per format.").
- `web/composer/tools/sources.py:1034-1040` `set_source_from_blobs` description
  ("…feeding aws_textract_inline_analysis; mixed formats need one homogeneous source per
  format.") — VERIFIED not golden-pinned (`test_tool_declarations.py` pins only
  `create_blob` and the singular declaration); one-file change.
- `examples/textract_inline/README.md:3-13` (lines 11-13 "Documents already stored in
  S3, multipage PDFs, or files over 5 MiB belong to … aws_textract_document_analysis"
  must be rewritten), `:41-46`; `examples/textract_inline/input/README.md`;
  `examples/AGENTS.md:189`.

NOT in scope (corrections): `web/plugin_policy/profiles.py:1188-1197` and
`tests/unit/web/catalog/test_policy_view.py:161-186` apply only to
`aws_textract_document_analysis` (`profiles.py:1079` registers the resolver for that one
id; inline has no `web_config_authority`).

## 5. `passes_through_input` and output fields

`:260` `passes_through_input = True`; `:584` `output = row.to_dict()` copies the whole
input row, so `document_id`/page number reach the sink automatically. Added fields are
author-named (`text_field`, `page_count_field`, `metadata_field`, `result_field`,
`extract.*`). Only config-vs-config duplicates are checked (`:225-227`), never
config-vs-inbound-row — a textract output name colliding with a pdf_rasterize field
would silently overwrite; document it. The success audit record (`:612-632`) carries no
page identity; per-page attribution comes from row pass-through only.

Page numbering recommendation: **`page_number`, 1-based** (Textract's own `"Page": 1`
vocabulary at `:649,:653`; query selector `["1"]`). `page_index` + 1-based is the
off-by-one trap; do not ship that pairing.

## 6. Existing example `examples/textract_inline/`

No static `settings.yaml`; `scripts/prepare_document_blobs.py` generates
`settings.generated.yaml` (gitignored); exempted at
`tests/e2e/examples/test_shipped_examples.py:69-77` (comment path is cosmetically wrong:
real path is `examples/textract_inline/scripts/prepare_document_blobs.py`). The script
requires format homogeneity (`:85-89`), rejects >5 MiB (`:67-68`). A future variant
(`blob_rows` → `pdf_rasterize` → `aws_textract_inline_analysis(document_format: png,
blob_ref_field: page_blob_ref)`) fits the generated-settings pattern and keeps the
exemption only if it stays inside this directory.

## 7. `_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS` — pdf_rasterize does NOT join

`web/interpretation_state.py:165-167` (`web_scrape`, both textract plugins); fail-open
by construction (`:160-164`). `_producer_reaches_untrusted` (`:527-548`) walks backward
from an LLM node; a listed plugin returns itself and STOPS (`:536-537`); a plain
transform falls through to its own upstream (`:548`).

Reasoning: pdf_rasterize surfaces no externally-controlled TEXT — it renders bytes
already in the row into an opaque PNG ref, nothing an LLM reads as instructions.
Decisively, membership would be unreachable in the designed shape
`pdf_rasterize → aws_textract_inline_analysis → llm`: the walk hits the textract plugin
first and stops there, so the untrusted verdict is already raised by the plugin that
converts pixels into prompt-bound text. Listing pdf_rasterize would change no verdict
where it is used as designed and would produce a FALSE untrusted verdict in a
`pdf_rasterize → llm` graph where no OCR ran. It is a fall-through node; `:548` handles
it correctly — a rasterized page inherits the trust class of whatever produced the PDF.

## 8. Registrations the new plugin must join

`boundary_expectations.py:148+` `EXPECTED_TRANSFORM_DETERMINISMS` (every transform,
production-code diff); determinism IO_READ keeps it out of the external-catalogue
sweep; `plugin_version` + `source_file_hash` are gated for `plugins/transforms/*.py`
(PH1/PH2/PH3); prose gates B/C/D apply to the plugin's own three strings.
