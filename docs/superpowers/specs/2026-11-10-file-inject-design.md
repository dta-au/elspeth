# `file_inject` transform — design

**Status:** implemented on `claude/happy-cray-mbfmm1` and merged onto the 0.8.12
code base (`fix/composer-remediation-0.8.12`).
**Target release:** 0.8.12.
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
| `text` | The whole content as one `str`, with no trimming. Line endings follow the binding path (see "Decisions taken during implementation"). | None. |
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
- a value the row hasher cannot canonicalise (an integer outside ±(2^53 − 1),
  or text that is not UTF-8 encodable), in any format and in `default_content`;
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

The planner attaches an uploaded blob in one of two ways, and no new tool is
added:

- for an entry that already exists, with the existing `wire_blob_inline_ref`
  tool at `node:<node_id>.options.files.<entry_name>.content`;
- when a call creates the node or the entry (`upsert_node`, `splice_transform`,
  `set_pipeline`, `patch_node_options`), by setting that entry's `content` to
  the marker `{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256":
  "<content_hash>"}` in the same call. An entry without `content` fails config
  validation, so a node cannot be created first and wired afterwards.

### Upload-only guard

Today `is_llm_authored_prompt_surface_binding` refuses an LLM-authored blob
bound into an `llm` node's `prompt_template`, `system_prompt`, `model` or query
templates, because the prompt review reads those options as strings and would
not see blob-substituted text. `file_inject` exists to feed prompts, so a
planner-written blob bound to its `content` would reach a prompt unreviewed.

The guarded domain is extended: only a file the user uploaded is accepted at
`files.<name>.content` (and at any marker at or beneath `files`) on a
`file_inject` node. A blob counts as user-uploaded only when it was created by
the user (`created_by == "user"`) and its creation modality is not
LLM-authored. Both are read as a pair because a blob the composer created from
text in the user's message, a pipeline output and a session-fork copy of either
all record a verbatim modality without being an upload; a fork copy keeps its
source's creator, so a copy of an untouched upload is still an upload. The existing `llm`
prompt-surface guard is unchanged and stays modality-only. The refusal is applied at the same three points as the
existing guard, through the same predicate module so the three cannot drift:

1. authoring — `wire_blob_inline_ref` and the node-option tools
   (`upsert_node`, `patch_node_options`, `splice_transform`, `set_pipeline`);
2. `/validate` materialization;
3. run admission.

**Known gap, closed 2026-11-11 by ruling: an agent write is an agent write.**
The creator-and-modality pair used to fail across a session fork and in one
same-session case. `update_blob` kept `created_by == "user"` on a rewritten
upload. `copy_blobs_for_fork` keeps the creator and resets the modality to
`VERBATIM` with null provenance, so the copy of planner-written content read as
a user upload. And an `update_blob` whose content is a contiguous substring of
the user's current message records `VERBATIM`, so the rewritten upload stayed
user/`VERBATIM`. The blob row has no structured field marking a fork copy, and
the copy cannot carry the source's LLM provenance
(`fk_blobs_created_from_message_session` binds `created_from_message_id` to the
copy's own session), so the fix is at the rewrite: `update_blob` now sets
`created_by = "assistant"` on any blob it rewrites, exactly as for a blob the
agent created new. A rewritten upload, and any fork copy of it
(assistant/`VERBATIM`), is refused by the existing predicate.

What this closes: a user upload rewritten by `update_blob` can no longer be
bound to `file_inject`, in the same session or after a fork. What remains open:
the rewrite is one-way (the original upload bytes are gone and the blob stays
the assistant's), and the `llm` prompt-surface guard is modality-only, so a
fork copy of a rewritten upload (assistant/`VERBATIM`) still resets that guard.
That guard is deliberately unchanged here.

Literal content is refused as well (decided 2026-11-10, after the transform was
built). The blob guard alone leaves a second route open: a composer authoring
tool can write literal text straight into `files.<name>.content` with no blob
involved, and unlike an `llm` prompt nothing reviews it. So in the composer, the
only accepted value at `files.<name>.content` on a `file_inject` node is an
`inline_content` marker for a user-uploaded blob. A literal string there, from
any node-option authoring tool, is refused at authoring, and a composition state
that carries one is refused at `/validate` and run admission. Literal `content`
in a hand-written settings file run from the CLI is unaffected: its author wrote
it.

Every refusal, for a blob the user did not upload and for literal content alike, is
repairable and steers the planner to the path that works, naming the actual node
id and entry name:

1. tell the user they need to save the text as a file and upload it;
2. once it is uploaded, find it with `list_blobs` / `get_blob_metadata`;
3. attach it: for an entry that already exists, with `wire_blob_inline_ref` at
   `node:<node_id>.options.files.<entry_name>.content`; when the call creates
   the node or the entry, by putting the marker for that upload in the entry's
   `content`.

An inline blob reference addresses option keys that start with a letter or `_`
and contain only letters, digits, `_` and `-`. An entry with any other name (a
dot, a space, a leading digit) can never hold an upload, so in the composer such
an entry is refused at the same points, and the refusal says to rename it. In
`mode: select` the entry name must equal the row's key value, so a key that
does not fit is normalised upstream.

`default_content` is refused in the composer in any form, at the same points,
and its refusal steers to `on_miss: null` or `on_miss: fail`.

The wording is specific to `file_inject`. It must not suggest planner-created
files are refused elsewhere: a planner-created source and planner-written prompt
text remain allowed behind their own review cards. Tests assert the steering
text on every tool that can trigger a refusal, and that the same call with a
user-uploaded blob then succeeds.

An approval card for planner-created `file_inject` content ("for the LLM to use
this you have to approve it", as for an invented source) is tracked separately
as dta-au/elspeth#361 and is out of scope here.

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

## Decisions taken during implementation

Recorded here so the spec matches what was built.

- **JSON nesting** deeper than 100 levels is refused (`MAX_FILE_INJECT_JSON_DEPTH`).
- **A CSV cell** longer than Python's per-cell limit (131,072 characters) is
  refused with a message that says so. The limit is process-wide and is not
  raised; longer text belongs in a `text` or `json` file.
- **`default_content`** must be canonical JSON, fits the per-entry cap and
  counts toward the per-node cap. `default_content: null` with
  `on_miss: default` is refused in favour of `on_miss: null`.
- **Entry names** must be quoted strings, non-empty, with no leading or trailing
  whitespace.
- **Collision with the declared input** means the output field is named in
  `required_input_fields` or equals `key_field`. Declaring a written field's
  type in the node's `schema` is allowed and is checked against the derived
  type.
- **A leading byte-order mark** is stripped before parsing `json`, `jsonl` and
  `csv`; `text` keeps it. The transform never alters `text` content, but the
  two binding paths deliver different strings for the same file: the CLI
  settings loader reads a `content_file` in text mode
  (`core/template_materialization.py`, shared with `reference_join` and the
  prompt files), so `\r\n` and `\r` become `\n`; a web upload is decoded
  byte-for-byte (`core/blobs_inline.py`) and keeps its line endings. The loader
  is not changed; the documentation states the difference.
- **Empty content**: `text` writes `""`, `jsonl` writes `[]`, and `json` and
  `csv` are refused.
- **Values the row hasher refuses are refused at config validation** (final
  review, 2026-11-11). A written value is hashed with every row that receives
  it, so an integer outside the JSON safe range or a string holding a lone
  surrogate used to pass validation and `/validate` and then fail every such
  row as a contract violation. The config walk now refuses both, in `json`,
  `jsonl`, `csv`, `text` and `default_content`, naming the entry. The number
  rule is `core.canonical.is_non_canonical_number`, the hasher's own
  predicate, not a second copy of the bound, and a test hashes values that
  validation accepts so the two cannot drift apart silently. A JSON integer
  too long for Python to convert (more than 4300 digits) is refused in the
  same words instead of surfacing the interpreter's advice.
- **An unreadable `content_file`** (a directory, or not UTF-8 text) is a
  configuration error naming the entry and the file. The rule is opt-in per
  registry entry (`unreadable_file_is_config_error`), so `reference_join`'s
  read faults propagate unchanged.
- **The catalogue example (`example_use`) binds files with `content_file`.**
  The example is carried to the planner in inventory results, and literal
  `content` is the form the composer refuses, so the example must not teach it.
- **Shared parsing helpers** live in `plugins/infrastructure/config_content.py`
  and raise neutral `ValueError` subclasses; `reference_join` wraps them to keep
  its existing exception types and text.
- **Attaching an upload when the node does not exist yet.** A `file_inject`
  entry without `content` fails config validation, and literal `content` is
  refused in the composer, so a node can only be created by a call that already
  carries the marker. Step 3 of the working path therefore has two forms: for an
  entry that already exists, `wire_blob_inline_ref` at
  `node:<node_id>.options.files.<entry_name>.content`; when the call creates the
  node or the entry, the entry's `content` is set to
  `{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}`
  in the same call. No new incomplete state was added. A wrong or stale hash
  fails closed at `/validate` and at run admission.
- **Entry names in the composer** must be inline-reference path segments
  (`[A-Za-z_][A-Za-z0-9_-]*`). Without this refusal a marker beneath, say,
  `"v1.2"` was stored by the authoring tools and `/validate` then raised on the
  field path. CLI settings accept any quoted, non-empty entry name as before.
- **"User-uploaded" is creator and modality together** (`created_by == "user"`
  and a modality that is not LLM-authored), for `file_inject` only.
- **`default_content` is refused in the composer** in any form (literal or
  marker), at authoring, YAML import, `/validate` and run admission: it is
  planner-authorable text written into rows on a miss. The refusal steers to
  `on_miss: null` or `on_miss: fail`. CLI settings are unaffected.
- **YAML import** refuses literal `content` and `default_content` on a
  `file_inject` node with HTTP 400; an `inline_content` marker is left
  untouched. YAML whose entry has no `content` imports, and the upload is then
  attached to that entry.
- **One error code**, `file_inject_content_not_uploaded`, covers the three
  refusal causes. The pipeline planner's feedback withholds validator messages,
  so the code carries static guidance with the same working path.
