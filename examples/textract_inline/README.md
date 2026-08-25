# Amazon Textract Inline Analysis

Analyze local JPEG, PNG, or single-page PDF documents with the synchronous
`aws_textract_inline_analysis` transform: each document is staged into the
example's isolated payload store, `blob_rows` emits one custody row per
document (payload hash plus bounded metadata — never bytes), and the
transform retrieves the bytes by content hash, verifies the declared format's
byte signature, and sends them to one audited `AnalyzeDocument` call per row.

This is the CLI twin of the Web Composer flow (upload/paste a document →
`set_source_from_blobs` → analyze). Documents already stored in S3 or over
5 MiB belong to the asynchronous `aws_textract_document_analysis` plugin. A
multipage PDF can instead be split by the `pdf_rasterize` transform into one
PNG page per row and analyzed inline page by page (set
`blob_ref_field: page_blob_ref` and `document_format: png` on the analysis
node).

## Prerequisites

- AWS credentials resolvable through boto3's **default chain** (environment,
  profile, or role) with `textract:AnalyzeDocument` allowed in the chosen
  region.
- **Every run makes billable Textract calls** — one `AnalyzeDocument` per
  document, and SDK/engine retries can each bill again (the synchronous API
  has no idempotency token). The per-row audit records the SDK attempt count.

## Run it

```bash
# 1. Copy one or more documents of ONE format into input/
cp ~/scans/invoice-page-1.png examples/textract_inline/input/

# 2. Stage them into the payload store and generate the pipeline
python examples/textract_inline/scripts/prepare_document_blobs.py --region ap-southeast-2

# 3. Execute (billable)
elspeth run --settings examples/textract_inline/settings.generated.yaml --execute

# 4. Inspect results and the audit trail
cat examples/textract_inline/output/textract_results.jsonl
elspeth explain --run latest --database examples/textract_inline/runs/audit.db
```

The prepare script verifies each document's byte signature, enforces the
5 MiB synchronous bound, and refuses mixed formats: one transform instance
declares exactly one `document_format`, and the runtime rejects any
signature/format disagreement fail-closed. To analyze several formats, stage
and run one format at a time (or author a branched pipeline with one
transform instance per format). A multipage PDF is not a supported input to
this script — rasterize it into per-page PNGs with `pdf_rasterize` first,
then stage the rendered pages the same way.

Row-level failures (provider rejections, page count other than one, malformed
responses) land in `output/textract_failures.jsonl` with sanitized,
categorical reasons; raw provider messages and document bytes never enter
rows, errors, or audit records — the payload hash in the call audit binds
each request to the exact integrity-checked source content.
