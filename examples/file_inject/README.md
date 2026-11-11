# file_inject — put a whole file on a row

A row arrives carrying a value that says what kind of thing it is, and nothing
about how that kind should be handled:

```csv
ticket_id,type,summary
T-1001,A,Parcel arrived with a crushed corner
```

The handling guidance for type `A` is a file. `file_inject` writes the whole
content of a bound file into a row field, so a later step reads it as ordinary
row data. The usual later step is an LLM prompt:

```yaml
prompt_template: |
  {{ row.guidance }}

  Ticket: {{ row.summary }}
```

This example stops before that step. It needs no LLM, no network and no
credentials: a CSV source, `file_inject`, and JSON Lines sinks.

## Run it

```bash
elspeth run --settings examples/file_inject/settings.yaml --execute
elspeth run --settings examples/file_inject/settings_all.yaml --execute
elspeth run --settings examples/file_inject/settings_miss.yaml --execute   # exits 1 BY DESIGN
```

| Config | Mode | Rows | Ends |
|---|---|---|---|
| `settings.yaml` | `select` on `type` | 4 | COMPLETED, exit 0 |
| `settings_all.yaml` | `all` | 4 | COMPLETED, exit 0 |
| `settings_miss.yaml` | `select` on `type`, one type with no entry | 3 | **PARTIAL, exit 1 by design**: 2 written, 1 original row routed |

## `mode: select` — one file per row, chosen by a row value

`settings.yaml` binds three files and picks one per row:

```yaml
mode: select
key_field: type          # the column on the ARRIVING ROW
output_field: guidance   # the NEW field the content is written to
files:
  "A": {content_file: guidance_a.md,   format: text}
  "B": {content_file: guidance_b.json, format: json, data_key: rubric}
  "C": {content_file: guidance_c.csv,  format: csv}
on_miss: fail
```

The entry names are the key values. They are compared exactly as written, with
no trimming and no case folding, and the row's key must be a string. Quote
them: YAML reads an unquoted `1:` or `true:` as a number or a boolean, and the
configuration is refused.

`output/tickets_with_guidance.jsonl` has one line per ticket. One row of each
type, as the run writes them:

Type `A`, a `text` entry. The value is the file as one string, with nothing
trimmed, down to its final newline. The settings loader reads a `content_file`
in text mode, so a file saved with Windows (`\r\n`) line endings arrives with
`\n`; the same file uploaded in Composer keeps its `\r\n`:

```json
{"guidance": "# Type A: delivery problems\n\nApologise once, in the first sentence.\nOffer a replacement or a refund and let the customer choose.\nDo not ask for photos unless the order is over $200.\n", "summary": "Parcel arrived with a crushed corner", "ticket_id": "T-1001", "type": "A"}
```

Type `B`, a `json` entry. The value is parsed, so `refund_limit` is a number
and `escalate` a boolean. `data_key: rubric` wrote that one member of the
file's top-level object; the file's `version` member is not on the row:

```json
{"guidance": {"escalate": true, "must_include": ["invoice number", "amount in dispute"], "refund_limit": 500, "tone": "precise"}, "summary": "Charged twice for the March invoice", "ticket_id": "T-1002", "type": "B"}
```

Type `C`, a `csv` entry. The value is a list with one object per data row,
keyed by the header. Cells stay strings: `step` is `"1"`, not `1`:

```json
{"guidance": [{"instruction": "Confirm the account email address", "step": "1"}, {"instruction": "Ask which device and browser they use", "step": "2"}, {"instruction": "Send the reset link again and stay on the ticket", "step": "3"}], "summary": "Cannot sign in after the password reset", "ticket_id": "T-1003", "type": "C"}
```

`format` is required on every entry. It is never inferred from the file name:
`guidance_a.md` is `text` because the settings say so. The fourth format,
`jsonl`, writes a list with one parsed value per non-blank line.

One field that holds a string on some rows, an object on others and a list on
the rest is what this example is built to show. It is rarely what a pipeline
wants. When `guidance` feeds a prompt, bind every entry as `text`: the field is
then declared `str`, where a mix of formats declares it `any`.

## `mode: all` — every file on every row

`settings_all.yaml` binds the same three files with no key. Each entry names
its own `output_field`, and every row gets all of them:

```yaml
mode: all
files:
  "style":     {content_file: guidance_a.md,   format: text, output_field: style_guide}
  "rules":     {content_file: guidance_b.json, format: json, output_field: review_rules}
  "checklist": {content_file: guidance_c.csv,  format: csv,  output_field: checklist}
```

Here the entry names are labels only. There is no key, so there is no miss and
no `on_miss`. One unconditional file is `mode: all` with a single entry.

The first of the four rows in `output/tickets_with_everything.jsonl`. The other
three differ only in `ticket_id`, `type` and `summary`. With no `data_key`, the
JSON file is written whole, `version` included:

```json
{"checklist": [{"instruction": "Confirm the account email address", "step": "1"}, {"instruction": "Ask which device and browser they use", "step": "2"}, {"instruction": "Send the reset link again and stay on the ticket", "step": "3"}], "review_rules": {"rubric": {"escalate": true, "must_include": ["invoice number", "amount in dispute"], "refund_limit": 500, "tone": "precise"}, "version": 3}, "style_guide": "# Type A: delivery problems\n\nApologise once, in the first sentence.\nOffer a replacement or a refund and let the customer choose.\nDo not ask for photos unless the order is over $200.\n", "summary": "Parcel arrived with a crushed corner", "ticket_id": "T-1001", "type": "A"}
```

## A row whose type names no entry

`settings_miss.yaml` reads `input_with_unknown.csv`, where ticket T-2002 has
type `D`. No entry is named `D`.

`on_miss` defaults to `fail`. The row becomes a per-row error and leaves
through the node's `on_error`, which this config points at the `unmatched`
sink. The run ends PARTIAL and the CLI exits 1. Its last line, where the total
time varies from run to run:

```text
⚠ Run PARTIAL: 3 rows processed | ✓2 succeeded | ✗1 failed | ⚠0 quarantined | →1 routed (unmatched:1) | 0.36s total
```

`output/unmatched_type_tickets.jsonl` holds the ticket exactly as it arrived,
with no `guidance` field at all:

```json
{"summary": "Asking about a partnership", "ticket_id": "T-2002", "type": "D"}
```

`output/known_type_tickets.jsonl` holds the other two, T-2001 and T-2003, with
their guidance. The audit database (`runs/audit_miss.db`) records why the row
was routed. The reason names the key field and the entry names, never the row's
value or any file content. As stored in `transform_errors.error_details_json`:

```json
{"error":"no file_inject entry named for the row's 'type'; entries: ['A', 'B', 'C']","field":"type","reason":"file_inject_miss"}
```

The other policies:

* `on_miss: null` writes `guidance: null` and the row continues. Use it only
  when something downstream checks for the null. A prompt rendered from a null
  looks like any other prompt at a sink.
* `on_miss: default` writes `default_content`, a literal value in the settings.
  This one is for settings files run from the CLI; the Web Composer does not
  accept `default_content`.

A row with no `type` field, or with a `type` that is not a string, is a row
error under every `on_miss`. That is a wiring fault, not a miss.

## The files are configuration

`content_file` is read by the settings loader, not by the transform. Each
file's text lands inside the node's options, and so inside the node identity
and the run's topology hash.

Edit `guidance_a.md` between a run and its resume and the resume is refused.
The message, cut short here at the ellipsis, goes on to explain why and to give
the two topology hashes:

```text
Cannot resume run <run_id>: Pipeline configuration changed since checkpoint was created. ...
```

Put the file back as it was and the same resume proceeds. Replay and verify do
not read the file at all: they use the content recorded with the original run.

Two consequences you will meet first:

* `content_file` resolves relative to **this directory**, not the repository
  root, and is confined to it. That is why the guidance files sit beside
  `settings.yaml` while the source is addressed as
  `examples/file_inject/input.csv`.
* Every file is parsed when the settings are validated. Malformed JSON, a CSV
  row with the wrong number of cells, or a missing file stops
  `elspeth validate`; no row is processed first.

Files are read as UTF-8. Parsed content is capped at 16 MiB per entry and
64 MiB per node. See the
[configuration reference](../../docs/reference/configuration.md#bound-files-as-row-fields)
for every option, limit and refusal.

## What this is not

Not a source, and not a way to read a file at run time. The files are fixed
when the run starts, and each must be named: there is no directory or glob
form.

To pick one **record** out of a keyed table rather than one whole file, use
[`reference_join`](../reference_join/). To turn a file's records into **rows**,
use a source, or `blob_csv_expand` / `blob_json_expand`.

## Files

| File | Role |
|---|---|
| `input.csv` | 4 tickets; every `type` has an entry |
| `input_with_unknown.csv` | 3 tickets; T-2002 has type `D`, which has no entry |
| `guidance_a.md` | Bound as `text` |
| `guidance_b.json` | Bound as `json`; `settings.yaml` takes its `rubric` member, `settings_all.yaml` the whole file |
| `guidance_c.csv` | Bound as `csv` |
| `output/` | Written by the runs |
| `runs/` | Landscape audit databases, one per config |
