# PDF Rasterize Example

Demonstrates the `pdf_rasterize` transform: rows carrying a payload-store PDF
reference are exploded into one PNG page row per page (an expand group), and
a malformed document is refused whole and quarantined via `on_error` instead
of silently dropped.

## What This Shows

```
source ─(pdf_manifest)─> rasterize ─┬─(pages)──────> pages sink
                                     └─(on_error)───> quarantine sink
```

- `report.pdf` is a valid 3-page mock PDF. `pdf_rasterize` renders it into 3
  PNG page rows, each carrying `page_blob_ref` (the rendered page's
  payload-store hash), `page_number`, `document_id` (the source PDF's hash),
  `page_mime_type`, `page_size_bytes`, `page_width_px`, `page_height_px`, and
  `page_text` (the page's text extracted via pdfium's text layer — no OCR,
  fully offline; `report.pdf`'s three pages yield `"Page 1"`, `"Page 2"`, and
  `"Page 3"`), alongside the manifest's original fields.
- `broken.pdf` is a malformed PDF (a header with no valid xref table).
  `pdf_rasterize` refuses the whole row with reason `pdf_malformed` and
  `on_error` routes it to the quarantine sink — the row keeps only its
  original manifest fields there; the failure reason is recorded in the
  Landscape audit trail, not the sink file.

## Running

```bash
./examples/pdf_rasterize/run.sh
```

The launcher stages `input/report.pdf` and `input/broken.pdf` into the
example's isolated payload store, writes `input/pdf_manifest.csv` (two rows:
`blob_ref` plus a `document_name` label), and executes `settings.yaml`.

## Output

- `output/pages.jsonl` — 3 rendered page rows from `report.pdf`
- `output/quarantine.jsonl` — 1 quarantined row for `broken.pdf`
- `runs/audit.db` — the Landscape audit trail, including the `pdf_malformed`
  reason for the quarantined row

This run ends `PARTIAL` with process exit 1 — 1 of 2 source rows fails and is
quarantined by design, matching `error_routing`'s and `deep_routing`'s
documented `on_error` demonstrations. All source rows still reach exactly one
terminal outcome: `report.pdf`'s row expands into 3 page rows in `pages`, and
`broken.pdf`'s row lands in `quarantine`.

## Mock PDFs

`input/report.pdf` and `input/broken.pdf` are committed, deterministic
fixtures built by `tests/fixtures/pdf_documents.py` — see
`input/README.md` for the regeneration command.

## Key Concepts

- **Expand groups**: one input row can become many output rows, each
  carrying the group's shared `document_id`.
- **on_error routing**: a whole-document refusal quarantines the row rather
  than dropping it or partially emitting pages.
- **Payload-store staging**: PDF bytes never appear in the row itself, only
  a content-addressed `blob_ref`; see `blob_transforms` for the general
  staging pattern this example reuses.
