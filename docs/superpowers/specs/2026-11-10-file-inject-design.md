# `file_inject` transform — design

**Status:** design approved in conversation 2026-11-10; awaiting review of this
written spec. No code written.
**Target release:** 0.8.11 (built on `release/0.8.11-candidate`; confirm before
any changelog or release-notes entry).
**Branch:** `claude/happy-cray-mbfmm1`.

## Goal

A pipeline author binds one or more files to a transform and the transform
writes each file's whole content into a row field. Once written, the field is
ordinary row data.

The driving use case: an LLM step downstream needs guidance text that depends on
the row. A row with `type: A` should be prompted with the A guidance, `type: B`
with the B guidance, and so on. A second use case binds several unrelated files
(say five) and puts all of them on every row under different field names.

Success means:

- A YAML author names files next to the settings file and gets their content on
  rows without writing a lookup table or an expression.
- A Web Composer session does the same with files the user uploaded.
- The content is part of the node's configuration, so a run and its resume or
  replay see the same content, and an edit between them is refused, not silently
  picked up.
- Text files arrive as strings; JSON and CSV files arrive parsed.

## Decisions taken

| Question | Decision |
|---|---|
| Existing plugin? | None fits. `reference_join` is always a keyed lookup with per-entry output expressions. |
| New plugin or a `reference_join` mode | **New transform**, `file_inject`. `reference_join` already has two mutually exclusive config forms. |
| How the file is bound | **Config-bound**, as `reference_join` does it: a `*_file` option on the CLI, `inline_content` blob substitution on the web. No runtime file read. |
| Value shape | **Format-aware**: `text`, `json`, `jsonl`, `csv`. |
| Selecting one file per row vs. injecting all | **Both**, as `mode: select` and `mode: all`. |
| Directory glob / file discovery | **No.** Each file is named. The composer or operator does the wiring; an "enumerate files" source may come later. |
| Web Composer default surface | **Add `file_inject`** to the default authorized plugin set. |
| Planner-written blobs as content | **Refused.** Only user-uploaded blobs may back `file_inject` content. The planner tells the user to save and upload the text, then attaches the upload. |
| Trust-tier findings | Fixed and signed when the broader release branch lands; not a blocker for this change. |

## Configuration

Plugin name `file_inject`, a row transform (one row in, one row out).

### `mode: select`

Writes the content of the one entry whose name equals the row's key value.

```yaml
- plugin: file_inject
  options:
    mode: select
    key_field: type
    output_field: guidance
    on_miss: fail            # fail | null | default
    files:
      A: {content_file: a.md,   format: text}
      B: {content_file: b.json, format: json, data_key: rubric}
      C: {content_file: c.csv,  format: csv,  delimiter: ","}
    schema: {mode: observed}
```

### `mode: all`

Writes every entry onto every row, each to its own field.

```yaml
- plugin: file_inject
  options:
    mode: all
    files:
      policy:   {content_file: policy.md,    format: text, output_field: policy_text}
      rubric:   {content_file: rubric.json,  format: json, output_field: rubric}
      examples: {content_file: examples.csv, format: csv,  output_field: examples}
    schema: {mode: observed}
```

A single unconditional file is `mode: all` with one entry.

### Options

Top level:

| Option | Applies to | Meaning |
|---|---|---|
| `mode` | both | `select` or `all`. Required; no default. |
| `files` | both | Mapping of entry name → entry. At least one entry. |
| `key_field` | `select` | Row field whose value names the entry. Required in `select`, refused in `all`. |
| `output_field` | `select` | Field the selected content is written to. Required in `select`, refused at top level in `all`. |
| `on_miss` | `select` | `fail` (default), `null` or `default`. Refused in `all`. |
| `default_content` | `select` | Literal value written when `on_miss: default`. Required then, refused otherwise. |

Per entry:

| Option | Meaning |
|---|---|
| `content_file` | CLI only. Path relative to the settings file. Replaced by `content` + `content_source` at load. |
| `content` | The file's text. Set by the loader or by web blob substitution; may also be written inline. |
| `content_source` | Provenance of `content` (the original path). Written by the loader. |
| `format` | `text`, `json`, `jsonl` or `csv`. Required; no inference from the file name. |
| `data_key` | `json` only. Take this member of a top-level object. |
| `delimiter` | `csv` only. Single character; default `,`. |
| `output_field` | `all` only. Required there, refused in `select`. |

Entry names in `select` mode are the key values and are compared as written.
In `all` mode they are labels for diagnostics and for the web option path.

### Formats

| `format` | Value written | Parsing rules |
|---|---|---|
| `text` | The whole content as one `str`, unmodified (no trimming). | None. |
| `json` | The parsed value: object, array or scalar. With `data_key`, that member. | NaN and Infinity refused; duplicate object members refused; `data_key` requires a top-level object containing it. |
| `jsonl` | A list with one parsed value per non-blank line. | Same per-line rules as `json`. |
| `csv` | A list of row dicts keyed by header. | Header row required; `csv` strict mode; duplicate or empty headers refused; a row whose cell count differs from the header refused; cells stay `str`. |

These reuse the rules the JSON source and `reference_join` already apply. Where
those are importable helpers they are imported; where they are private to
another plugin they are moved to a shared module, not copied.

### Declared output types

Each written field is declared in the node's output schema and guaranteed.

- `all`: a `text` entry declares `str`; any other format declares `any`.
- `select`: `str` when every entry is `text` and `on_miss` cannot write a
  non-string; otherwise `any`. Nullable when `on_miss: null`.

An author's explicit schema declaration for a written field is checked against
this, as `reference_join` checks its joined fields.

### Validation refusals

All of these fail config validation, before any row is processed:

- a `content_file` that is missing, outside the settings directory, or given
  together with `content`;
- an entry with neither `content` nor `content_file`;
- content that does not parse under its `format`;
- a format option on the wrong format (`data_key` on `csv`, and so on);
- two entries writing the same `output_field`;
- an `output_field` the node's declared input schema already carries
  (where the input schema is observed and so unknown at validation, the
  collision is a row error at run time instead);
- an option that belongs to the other mode;
- content over the size caps below.

### Size caps

- Per entry: canonical size of the parsed value ≤ 16 MiB.
- Per node: sum over entries ≤ 64 MiB (the bound `reference_join` uses for its
  index).
- On the web the existing inline blob caps apply first: 256 KiB per reference,
  1 MiB aggregate.

The per-node cap counts every entry in both modes; in `all` mode that is the
amount added to every row.

## Components

| Piece | Location | Change |
|---|---|---|
| Transform | new `src/elspeth/plugins/transforms/file_inject.py` | Config models, parsing, per-row write. `determinism = DETERMINISTIC`. |
| File binding | `src/elspeth/core/template_materialization.py` | New registry entry: `content_file` → `content` + `content_source`, `content_kind="text"`. |
| Nested resolution | same file | The walk that resolves `tables.<name>.reference_file` is hard-coded to `plugin == "reference_join"` in `_materialize_plugin_config` and `reject_file_backed_options`. Replace both with a declared nesting on the registry entry (plugin name + container option key), used by `reference_join.tables` and `file_inject.files` alike. `content_file` is nested-only: it has no top-level form. |
| Run-mode table | `src/elspeth/plugins/infrastructure/run_mode_capabilities.py` | Register `file_inject`. |
| Audit readiness | `src/elspeth/web/audit_readiness/boundary_expectations.py` | `file_inject` → `DETERMINISTIC`, with the same reasoning as `reference_join`. |
| Web policy | `src/elspeth/web/plugin_policy/compiler.py` | Add `PluginId("transform", "file_inject")` to the default authorized set. |
| Upload-only guard | `src/elspeth/web/execution/_validation_materialization.py` and its three callers | See below. |
| Planner guidance | `src/elspeth/web/composer/skills/pipeline_composer.md`, `pipeline_capabilities.md` | See below. |

`content` is marked `EmittedToOutput`, as `reference_content` is, because its
value reaches row data.

## Data flow

1. **Load.** CLI: the settings loader reads each `files.<name>.content_file` as
   UTF-8, confined to the settings directory, and replaces it with `content` and
   `content_source`. Web: blob substitution writes the uploaded file's text into
   `files.<name>.content`.
2. **Validate.** The config model parses every entry once into its final value,
   applies the refusals and caps above, and retains the parsed values.
3. **Run.** Per row the transform only copies. `all` writes each parsed value to
   its field. `select` reads `key_field`, finds the entry of that name and
   writes its value to `output_field`. Every row receives its own deep copy of a
   non-string value, so a downstream mutation on one row cannot reach another.
4. **Resume and replay.** `content` is in the node's options, so it is in node
   identity and the topology hash. Editing a bound file between a run and its
   resume refuses the resume. Replay and verify take `content` from the source
   run's recorded options and do not read the file; this is the existing
   behaviour of the file-option registry and needs no new code beyond the
   nested walk carrying it.

## Row-level failure behaviour (`select`)

| Situation | Result |
|---|---|
| Key value names an entry | That entry's value is written. |
| Key value is a string naming no entry | Per `on_miss`: `fail` returns a row error (routed by the node's error route); `null` writes `None`; `default` writes `default_content`. |
| `key_field` absent from the row | Row error regardless of `on_miss`. A missing key field is a wiring fault, not a miss. |
| Key value is not a `str` (including `None`) | Row error regardless of `on_miss`. No coercion: `1` does not match an entry named `"1"`. |

Matching is exact: no trimming, no case folding. Row errors use
`TransformErrorReason` with a `file_inject`-specific reason and carry the key
field name and the sorted entry names, never the content.

`all` mode has no row-level failure of its own beyond an output-field collision
with an observed input field.

## Web Composer

### Wiring

The planner attaches an uploaded blob with the existing `wire_blob_inline_ref`
tool at `node:<node_id>.options.files.<entry_name>.content`. No new tool.

### Upload-only guard

Today `is_llm_authored_prompt_surface_binding` refuses an LLM-authored blob
bound into an `llm` node's `prompt_template`, `system_prompt`, `model` or query
templates, because the prompt review reads those options as strings and would
not see blob-substituted text. `file_inject` exists to feed prompts, so a
planner-written blob bound to its `content` would reach a prompt unreviewed.

The guarded domain is extended: a blob whose creation modality is LLM-authored
is refused at `files.<name>.content` (and at any marker at or beneath `files`)
on a `file_inject` node. The refusal is applied at the same three points as the
existing guard, through the same predicate module so the three cannot drift:

1. authoring — `wire_blob_inline_ref` and the node-option tools
   (`upsert_node`, `patch_node_options`, `splice_transform`, `set_pipeline`);
2. `/validate` materialization;
3. run admission.

The refusal text is repairable and tells the planner what to do: ask the user to
save the text as a file and upload it, then attach the upload.

This is validation and rejection of planner output, which the composer
invariants permit. It adds no server-authored structure and no tutorial-specific
path.

`reference_join.reference_content` has the same indirect route and stays
unguarded in this change; it is raised as a separate issue.

### Planner guidance

The skill text gains a short `file_inject` passage stating:

- what the transform is for and the two modes;
- the option path to wire an uploaded blob into;
- that `format` must be stated per entry;
- that only user-uploaded files can be attached, and that when the user wants
  text they have not uploaded, the agent must tell the user to save it as a file
  and upload it, and attach it only once the upload exists. The agent must not
  create the blob itself as a substitute.

## Compatibility

`file_inject` is additive. One existing behaviour is touched: the nested
`reference_file` walk is re-expressed through the registry. Its observable
behaviour for `reference_join` — resolved options, error text, replay rules —
must not change, and the existing loader tests for it are the proof.

## Testing

Tests are written before the code they cover.

| Layer | Proves |
|---|---|
| Unit — config | Each format yields the stated value; every validation refusal above; declared output types for each mode and `on_miss`. |
| Unit — transform | `all` writes every field; `select` writes the matching entry; each `on_miss` policy; missing and non-string key are row errors; values are not shared between rows. |
| Unit — loader | `files.<name>.content_file` resolves; containment; `content` + `content_file` refused; replay uses recorded content; in-memory web loading rejects `content_file`. Existing `reference_join` materialization tests stay green unchanged. |
| Unit — web guard | LLM-authored blob refused at authoring, `/validate` and run admission; each refusal has a control showing the same call accepted with a user-uploaded blob. |
| Unit — web policy / catalogue | `file_inject` is in the default surface and its knob schema golden file exists. |
| Integration | YAML pipeline: source → `file_inject` (`select` on `type`) → LLM template reading the field → sink; resume refused after a bound file is edited. |
| Example | `examples/file_inject/` with one `all` and one `select` settings file, covered by the shipped-examples test. |

A new plugin also moves the exact pins listed in CONTRIBUTING.md § "Gate: plugin
inventories, source hashes, scenario-corpus manifest, fingerprint baseline": the
transform count, catalogue reference and metadata tests, the plugin source hash,
the state-engine lifecycle matrix and its golden file, and the validation-path
agreement test. Each is regenerated from the live registry, not edited by hand.

Checks before completion: the focused tests; the full default `pytest tests/`
(plugin registration and the shared settings loader change); ruff; mypy; the
keyless trust-tier lint compared before and after. The PostgreSQL testcontainer
suite is not run: no schema, SQL, persistence or lock is touched.

## Documentation

- `docs/reference/configuration.md`, `docs/guides/yaml-authoring.md` — options
  and the two modes.
- `docs/guides/user-manual.md` — upload, then attach, on the web.
- `examples/README.md`, `examples/AGENTS.md`, `examples/file_inject/README.md`.

## Out of scope

- Directory globs and an "enumerate files" source.
- `encoding` (content is text by the time the plugin sees it; the CLI loader
  reads UTF-8), `skip_rows`, and headerless CSV with `columns`.
- Binary files.
- Guarding `reference_join.reference_content` against planner-written blobs.
- Operator signing of any trust-tier allowlist entries.
- Changelog and release-notes entries, until the target release is confirmed.
