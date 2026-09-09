# Blob Transform Examples

This folder shows the blob-ingress pattern — and the four expander plugins that
turn ingested bytes into rows — without adding an end-to-end test that depends
on live network behaviour.

## The two arms

Every expander answers the same question, "where do the bytes come from?", in
one of two ways. The distinction is the point of this example, and it is easy
to miss because only one half is widely known:

| Arm | How it is configured | Where the bytes live |
|-----|----------------------|----------------------|
| **blob** | `source: blob` (the default), `blob_ref_field: blob_ref` | the payload store, addressed by a content hash an upstream `blob_fetch` produced |
| **inline** | `source: field`, `text_field: <column>` | a plain column of the row, already text — **no payload store involved at all** |

Both arms run the *same* parser with the *same* options and the *same* error
taxonomy. Only the read differs. The two inline configs here have no
`payload_store:` block whatsoever, which is the clearest way to see it.

## The five offline configs

| Config | Plugin | Arm | Rows |
|--------|--------|-----|------|
| `settings_expand_csv_blobs.yaml` | `blob_csv_expand` | blob | 2 → 200 |
| `settings_expand_json_blobs.yaml` | `blob_json_expand` | blob | 2 → 3 |
| `settings_expand_text_blobs.yaml` | `blob_text_expand` | blob | 2 → 5 |
| `settings_expand_inline_csv.yaml` | `blob_csv_expand` | inline | 2 → 5 |
| `settings_expand_inline_json.yaml` | `blob_json_expand` | inline | 2 → 3 |

All five are clean fixtures and end **COMPLETED / exit 0**. A non-zero exit from
any of them is a real defect, not a by-design partial.

Run all five:

```bash
./examples/blob_transforms/run.sh
```

The launcher stages the fixtures, then runs each config against its own
**expected exit code** rather than a blanket `-eq 0` — see the "Exit 0 is not
the corpus gate" section of `examples/AGENTS.md` for why that matters.

Run one at a time instead:

```bash
python examples/blob_transforms/scripts/prepare_csv_blob_manifest.py   # blob arm only
python examples/blob_transforms/scripts/prepare_expander_blobs.py      # blob arm only
elspeth run --settings examples/blob_transforms/settings_expand_text_blobs.yaml --execute
```

The two inline configs need **no staging step** — their input is a committed
CSV file. That is not a shortcut taken for the example; it is what the inline
arm is.

## Why `blob_text_expand` exists

Prose is not a CSV with one column, and treating it as one loses data. Both
outcomes below are measured against these plugins, expanding a single line of
text; only the second one is loud.

**A wholly quoted line is silently corrupted.** Input bytes
`"Water security remains the binding constraint."\n`:

| Plugin | Status | Emitted `line` |
|--------|--------|----------------|
| `blob_csv_expand` (`columns: [line]`) | `success` | `Water security remains the binding constraint.` |
| `blob_text_expand` | `success` | `"Water security remains the binding constraint."` |

The quotes are gone, and the row reports **success**. `csv.reader` read them as
field delimiters, because that is what they mean in CSV. Nothing anywhere in the
pipeline can tell that the text was altered.

**A quoted clause followed by prose fails outright.** Input bytes
`"It's fine, nothing special," the reviewer wrote.\n`:

| Plugin | Status | Result |
|--------|--------|--------|
| `blob_csv_expand` (`columns: [line]`) | `error` | `csv_parse_error: ',' expected after '"'` |
| `blob_text_expand` | `success` | the line, byte for byte |

So CSV-over-prose either destroys the text quietly or refuses it noisily,
depending on where the quotes fall. `blob_text_expand` reads bytes as text and
never parses a field grammar, so neither happens.

### `index_field` is a position in the blob, not a row counter

`prose_notes.txt` has a blank line in the middle, and the config sets
`skip_blank_lines: true`. The three emitted rows carry `line_index` **0, 2, 3**
— the gap at 1 is the dropped blank line. The index answers "where in the
document did this come from?", which survives filtering; it is not the ordinal
of the emitted row.

## What the JSON expander adds

`settings_expand_json_blobs.yaml` (blob arm) and
`settings_expand_inline_json.yaml` (inline arm) differ in two ways beyond the
arm itself, both visible in the files:

- **Format inference.** The blob arm omits `format` and infers it from the
  stored `blob_content_type` (`application/json`), fail-closed — an
  unrecognised content type is refused with a message naming `format` as the
  remedy. The inline arm **must** set `format`: a row field carries no content
  type, and nothing guesses.
- **Record selection.** The blob arm uses `data_key: documents` to pull the
  record array out of an enclosing object. The inline fixture's top level *is*
  the array, so it sets no `data_key`.

Nested values arrive as **real Python/JSON values**, not JSON-looking strings.
In `output/expanded_inline_json_rows.jsonl`, `"tags": ["infrastructure",
"budget"]` is a real list, ready for `json_explode` — not the JSON-looking
string it was parsed out of. The `json_text` column it came from is *not* on
that line: the inline arm consumes it, for the reason the next section gives.

## What survives the expansion, and what the two arms do differently

The blob arm reads a **hash**; the inline arm reads the **document itself**. That
one difference decides what reaches the output, and the two arms are deliberately
not symmetric.

**Blob arm — nothing is consumed.** `blob_ref` is a 64-character hash, so keeping
it on every emitted row costs almost nothing and is worth a lot: it is what keeps
rows from different source blobs disambiguated, and what ties an output row back
to the exact bytes in the payload store. These plugins declare
`passes_through_input: true`, meaning *every* input field survives. That is why
`settings_expand_csv_blobs.yaml` lists `blob_ref` in its sink schema.

**Inline arm — the source column is consumed and dropped.** The whole CSV or JSON
document lives in that column, so copying it onto every emitted row multiplies the
source bytes by the expansion factor — and it lands in the audit payload store,
not just memory. A 5,905-character document expanding to 500 rows would persist
2,952,500 characters to say nothing new. So the inline arm drops it, exactly as
`line_explode` and `json_explode` drop the row field they consume.

Look at `output/expanded_inline_csv_rows.csv`: five rows, one line each, no
embedded document. `output/expanded_inline_json_rows.jsonl` carries no
`json_text` for the same reason. Every *other* upstream column (`record_id`,
`source_label`) still survives — that half matters as much as the removal, and
is exactly what `forwards_input_fields` exists to keep visible.

That distinction has a name in the plugin contract, and it is not a detail:

- `passes_through_input` is **all-or-nothing** — it promises every input field
  survives, and the executor enforces it per row. A transform that drops one
  column cannot declare it; doing so anyway raises a Tier-1
  `PassThroughContractViolation` on every row.
- `forwards_input_fields` + `removed_input_fields` is the pair that *can* say
  "everything except this one column". It exists because transforms in exactly
  this shape were invisible to the graph's field walks, which hid upstream
  columns from a locked (`extra: forbid`) downstream consumer — the pipeline
  built green and every row died at the consumer's preflight.

Both declarations are per-arm on one plugin: the inline instance swaps to the
pair, the blob instance keeps the stronger promise.

**So the sink schemas differ, and the direction is easy to get backwards.**
`settings_expand_inline_csv.yaml` must NOT list `csv_text` — a fixed sink schema
demands every declared field be present, and that column no longer arrives.
Listing it is a build-time graph error. This inverted when the inline arm
started consuming its column: the same schema previously HAD to list `csv_text`,
and omitting it failed with `Extra fields forbidden by consumer: csv_text`. The
inline JSON sink sidesteps the question by declaring `mode: observed`, which
takes the emitted shape as it comes. The blob configs are the reverse: they
must list `blob_ref`, because it does arrive. Neither is a quietly narrower file;
both fail loudly at build time, which is the intended behaviour.

## Hosted Tutorial HTML Fetch

This example uses the public GitHub Pages tutorial files:

- `https://dta-au.github.io/elspeth/tutorial-site/project-1.html`
- `https://dta-au.github.io/elspeth/tutorial-site/project-2.html`
- `https://dta-au.github.io/elspeth/tutorial-site/project-3.html`

Run:

```bash
./examples/blob_transforms/run_hosted_fetch.sh
```

The launcher makes the payload-store root private (`0700`) before execution,
so the example also works in checkouts created under a collaborative umask.

Output:

- `examples/blob_transforms/output/tutorial_html_blobs.jsonl`

This configuration explicitly adds `text/html` to `allowed_content_types` because
`blob_fetch` is a generic blob fetcher, not an HTML scraper. The web-authored
path still blocks private-network allowlists; this example uses the default
`public_only` SSRF policy. It deliberately stops at blob refs; HTML parsing
stays with `web_scrape`.
