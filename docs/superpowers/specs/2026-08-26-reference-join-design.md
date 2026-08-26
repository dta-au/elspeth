# reference_join — enrich a row from a keyed reference table

Date: 2026-08-26
Status: design proposed, awaiting review. No implementation started.

## Problem

A pipeline row carries a business key and nothing else about it:

```csv
order_id,product,price
a,hats,$2.00
```

The description, tax rate, category and supplier of `hats` live in a separate
product-definitions table. ELSPETH has no way to get them onto the row.

Nothing in the tree does this today. Confirmed against the live registry:

* **Sources cannot.** Multiple sources exist (ADR-025), but every source fans
  into `NodeType.QUEUE` and the streams become ONE row stream. There is no
  side-input edge, no broadcast edge, and no node kind that materializes a
  table for another node to read.
* **Collectors cannot.** A collector is an EXPAND-group closer bound to an
  opener, not a table materializer.
* **The blob-expand family deliberately will not.** `blob_fetch →
  blob_csv_expand` (and `blob_json_expand`, in flight) is 1→N *row* fan-out by
  contract; its owner states there is "no intent for the blob-expand family to
  materialize anything but rows". Using it for reference data puts every
  reference row into the main line as a token that traverses the DAG and lands
  in the audit trail — the wrong shape for a lookup table — and you would then
  need a join to get the values back onto the main line. That join is this
  plugin. The chains are complementary, not redundant.
* **`llm`'s `lookup` option is a different thing.** It is a static YAML dict
  exposed to prompt templates as `{{ lookup.key }}`
  (`plugins/transforms/llm/base.py:159`, `_PROMPT_CONTEXT_NAMES:31`). It is not
  row-keyed and it is not a join. The name is taken, which is why this plugin
  is `reference_join`.

## What it does

One transform. Match a row field against a key column in a reference table;
lift one or more values out of the matched entry onto the row as named fields.

CSV-flat is the degenerate case of JSON-nested, so a single plugin with
path-addressed values covers both halves of the original request rather than
shipping two plugins that differ only in a parser.

```yaml
transforms:
  - name: enrich_products
    plugin: reference_join
    options:
      reference_file: products.csv        # CLI only — see "Table binding"
      reference_format: csv               # csv | json — required, never inferred
      key_field: product                  # ROW field to match on
      reference_key_name: sku                  # key WITHIN the reference table
      output:
        product_description: "ref['description']"
        product_tax_rate: "ref['tax']['rate']"
      on_miss: fail                       # fail | null | default
      default_values: {}                  # only when on_miss: default
```

## Design decisions

### Table binding: config-materialized content, on both surfaces

The table is bound directly on the transform — from a file on the CLI, from a
blob on the web. A side-input edge from a declared source was considered and
**rejected**, not deferred: it would need a new node kind, a new edge kind, a
load barrier and a DAG parity sweep, to deliver the same values.

The mechanism already exists and is tested. `reference_file` registers in
`FILE_BACKED_TEMPLATE_OPTION_REGISTRY`
(`core/template_materialization.py:46-68`) as:

| field | value |
|---|---|
| `file_key` | `reference_file` |
| `content_key` | `reference_content` |
| `source_key` | `reference_source` |
| `content_kind` | `text` |

At settings load, `materialize_options` (`:109-120`) POPS `reference_file`,
containment-checks the path against the config directory
(`_resolve_template_path`, `:72-91`), reads the bytes, and writes the **content**
into `reference_content` and the path string into `reference_source`.

Registering rather than inventing a private option buys three things at once:

1. **Reproducibility across resume.** Because content lands *in the config*, it
   flows into node identity (`builder.py:245`) and the full topology hash
   (`canonical.py:245-264`). Edit `products.csv` between a run and its resume
   and the topology hash differs, so resume is REFUSED (`resume.py:960-966`).
   This matters more than it looks: resume never re-reads a source — every
   source is swapped to `NullSource` (`cli.py:2757-2777`) and rows replay from
   the content-addressed payload store — while resume DOES re-execute
   transforms (`process_existing_row`, `resume.py:355-379`). A transform that
   stored a path and read bytes at runtime would be the first thing in the tree
   with neither protection. Storing content closes that by construction.
2. **Path traversal blocked** by the existing containment check.
3. **The web surface cannot see a path at all.** `reject_file_backed_options`
   (`:122-143`) raises for `load_settings_from_yaml_string()`, the in-memory
   web loader, on any registered file key. This is not a limitation to work
   around — it is the guard that closes the trap in the next section.

### Registering a fourth key trips a documented tripwire — handle it in the same commit

`PLUGIN_OPTION_COLLECTIONS` deliberately EXCLUDES `collectors`
(`core/template_materialization.py:15-30`, elspeth-ca79b2c63a), and the
exclusion rests on a premise this change falsifies: "All three file-backed keys
… belong to the LLM transform, and `LLMTransform` is not batch-aware." It
carries an explicit REACTIVATION TRIGGER: add `collectors` the moment any
batch-aware plugin declares a file-backed option field.

**`reference_join` does not fire that trigger.** It is a per-row enricher and
leaves `is_batch_aware` at its default `False`
(`plugins/infrastructure/base.py:396`), so it is not collector-legal, and on a
legal collector `reference_file` remains an extra that `extra="forbid"` rejects
at config load. The containment holds. `PLUGIN_OPTION_COLLECTIONS` is NOT
changed by this work.

Two things must still change in the same commit, because registration makes the
existing prose false:

1. **The exclusion comment.** "All three file-backed keys belong to the LLM
   transform" stops being true. Rewrite it to state the property that actually
   holds — no REGISTERED file-backed key belongs to a batch-aware plugin — and
   name `reference_join` as the first registered key outside the LLM transform,
   so the next reader does not re-derive the premise from a stale sentence.
   AGENTS.md requires a new convention to land with its change.
2. **The rejection message.** `reject_file_backed_options` hand-enumerates
   "inline prompt_template, lookup, and system_prompt" (`:142`) while
   *detecting* via the registry. That is the guard-restates-its-authority smell
   this spec cites elsewhere, and after registration the message omits our key
   while the check catches it. Derive the remediation list from
   `FILE_BACKED_TEMPLATE_OPTION_REGISTRY` rather than adding a fourth word by
   hand.

**Pinning this so it cannot rot:** if `reference_join` ever becomes
batch-aware, `collectors` must be added to `PLUGIN_OPTION_COLLECTIONS` in that
same change. That is the trigger the original comment describes, now with a
second plugin able to pull it.

### The trap this closes, stated explicitly

Web path confinement is a **key-name allowlist**, not type-driven:
`web/paths.py:13-19` enumerates the literal option names `path`, `file`,
`persist_directory`. Nothing performs `isinstance(config, PathConfig)`. A new
transform option holding a filesystem path under any other key name is
therefore unguarded by all four enforcement layers — an unregistered
`reference_file:` would be silently unconfined on the web surface.

Registration is what closes it: `reject_file_backed_options` keys on option
name via `FILE_BACKED_TEMPLATE_OPTION_KEYS`, so a registered `reference_file`
is refused outright by the web loader and never reaches a confinement check.
An *un*registered file option would get neither the web block nor the CLI
expansion. Extending `web/paths.py`'s hand-list was the alternative and is
rejected: a guard that restates its authority by hand is the smell this repo
has been burned by three times.

### Web binding: `inline_content` blob substitution

The web surface binds `reference_content` directly, using the existing
`mode: "inline_content"` blob mechanism. The field-path grammar
(`contracts/blobs_inline.py:37-40`) explicitly admits node options:

```
^(?:source(?::[^.\[\]]+)?|node:[^.\[\]]+|output:[^.\[\]]+)\.options(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$
```

so `node:enrich_products.options.reference_content` is a legal reference, and
the runtime walks `_NODE_COLLECTION_KEYS`, which includes `transforms`
(`core/blobs_inline.py:62`). Content is sha256-verified before substitution.

`bind_source` is NOT usable here: those markers are source-options-only
(`core/blobs_inline.py:660-661`) and are forbidden from carrying a sha256
(`contracts/blobs_inline.py:76-77`).

### `reference_source` is audit provenance, and is never opened

The registry writes the path string into `reference_source`, so the config
model carries that field, optional and defaulting to `None` (the web path never
sets it). It follows the `lookup_source` precedent exactly: llm emits
`<response_field>_lookup_source` as audit metadata described as "Config file
path" (`plugins/transforms/llm/__init__.py:29-30,91-92,286-319`) and never
reopens it. `reference_source` is likewise **inert as a path** — nothing
resolves it, reads it, or confines it, because nothing needs to.

Stated so nobody over-reads the registration claim: `reference_source` is a
`source_key`, not a `file_key`, so it is NOT in
`FILE_BACKED_TEMPLATE_OPTION_KEYS`, and `reject_file_backed_options` does not
block a web-authored config from setting it directly. That is acceptable
precisely because it is never opened — but it IS emitted, so an
operator-supplied string reaches output. It therefore needs the
`EmittedToOutput` marker, on the base class, like any other emitted option
value.

### The option takes RAW TEXT and the plugin parses it

This is the single most consequential decision, and it exists because the two
delivery paths hand the same field different types:

* `_substitute_at_path` writes a **string** (`core/blobs_inline.py:440`).
* `_load_content` under `content_kind="yaml"` writes a **parsed dict**
  (`core/template_materialization.py:157-161`).

One pydantic field cannot be both. Registering with `content_kind="text"` makes
`_load_content` return `file_path.read_text()` (`:155-156`) — a `str`, byte-identical
in type to what web substitution delivers. Parsing moves inside the plugin,
where a malformed table is a `PluginConfigError` whose wording we control.

Consequence: **no `ContentKind` extension is needed.** No new `csv`/`json`
kinds, no sweep of `ContentKind` dispatch sites.

### Reference table shape: a list of entries, in both formats

`reference_format: csv` parses to a list of flat dicts, one per data row, keyed
by header.

`reference_format: json` requires a **top-level JSON array of objects** — the
same list-of-entries shape, with values allowed to nest. `reference_key_name` names
a field within each entry in both formats, so the two differ only in whether
values can nest.

A top-level JSON *object* is REJECTED at load with an error naming the array
form, even though a key→entry mapping is a plausible reading of "JSON lookup
table". Admitting both would make `reference_key_name` meaningful in one shape and
meaningless in the other, and the reader would have to infer which from the
data. One shape, one meaning.

### Determinism: DETERMINISTIC, not IO_READ

The transform reads no file and makes no call at runtime; the table is already
in its config. This is a genuine difference from `blob_csv_expand`
(`IO_READ`, fetches at runtime) and it preserves the pipeline's reproducibility
grade (`reproducibility.py:41-46`), where `IO_READ` is graded
indistinguishably from `EXTERNAL_CALL` and `NON_DETERMINISTIC`.

Note that `Determinism` is purely descriptive — registration checks existence
only (`base.py:612`) — so this claim buys grading, not enforcement. It has to be
true because we designed it true, not because a gate holds it.

### Trust tier: the row keeps its source tier; the join does not re-tier

Reference-table bytes are operator-supplied — on the CLI a file inside the
config directory, on the web a blob uploaded by an authenticated user through
an authorized path — arriving through the same config-load ingress that
`lookup_file`, `template_file` and `system_prompt_file` already use and that
ADR-021 accepts. The transform itself performs no ingress.

Trust tier belongs to the ROW and is set at the SOURCE. `reference_join` is a
parser, and **a parser never re-tiers**. Joined-in values inherit the row's
existing tier unchanged. The plugin declares no `@trust_boundary`, because it
is not one.

### Miss policy: `on_miss`, defaulting to `fail`

`on_miss: fail | null | default`, default **`fail`**. A miss is a per-row VALUE
failure — `TransformResult.error`, routed by `on_error` — which is why
`reference_join` inherits `TransformDataConfig` (which carries `on_error`) and
not `DataPluginConfig` (which has no value-level failure mode).

Failing by default is the important half: an unenriched row reaching a sink is
indistinguishable from an enriched one, so a silent default would put
unattributable rows in the audit trail.

**Disambiguation, stated so it cannot be read two ways:** an output path that
does not resolve inside an entry that DID match is treated identically to a key
miss, per output field. `on_miss: fail` fails the row; `on_miss: null` nulls
that one field and keeps the rest; `on_miss: default` substitutes that field's
entry from `default_values`. One policy covers both cases; there is no second
knob.

`default_values` keys are validated at construction as a SUBSET of `output`
keys. An unmatched key would be a silent no-op — the exact failure mode this
design refuses everywhere else.

### Duplicate keys: rejected at load, no opt-out

Two entries sharing a `reference_key_name` value raise `PluginConfigError` at config
validation, naming the key and the offending entry positions. There is no
"first wins" or "last wins" option, because either makes output depend on file
ordering, which makes the run unreproducible — the property the whole binding
design above exists to protect.

### Value addressing: one grammar, the existing one

Output map values are expressions evaluated by the existing `ExpressionParser`
with the matched entry bound to the single name `ref`. CSV entries are flat
(`ref['description']`); JSON entries may nest (`ref['tax']['rate']`).

This works without touching the parser. `allowed_names` is a constructor
parameter, not a fixed set (`core/expression_parser.py:774-789`) — it defaults
to `["row"]`, but commencement gates already pass
`["collections", "dependency_runs", "env"]`. We pass `["ref"]`. Single-name
mode makes the bound context BE the value (`:495`, `:510-516`), so evaluating
`ref['description']` against the matched entry dict is exactly the shape the
evaluator already supports.

(Note for anyone re-deriving this: the parser's own docstrings at `:202` and
`:739` illustrate with `row`, and `llm`'s prompt context IS a closed frozenset
(`_PROMPT_CONTEXT_NAMES`). Neither generalizes to `ExpressionParser`, whose
name set is open by construction.)

This is deliberately more verbose than a bare `description`. The trade is
accepted: allowing a bare name "when it is flat" and an expression "when it is
nested" would be a second addressing grammar in a system that already has one,
and the boundary between the two forms would have to be guessed by the reader.

### Declared output fields

The `output:` map ENUMERATES its targets in config, so they are statically
declarable. Declare them typed `any`, following `value_transform`, which
`guarantees.py:610-616` names as the precedent for a transform that rewrites
fields and declares its targets as `any`.

This does NOT hit `blob_csv_expand`'s abstention hole (`guarantees.py:617-623`):
that exists because its columns are DATA-DERIVED from CSV headers and it can
build an empty-tuple shape. Ours are named in config and can never be empty —
an empty `output:` map is a config error.

### Construction-time collision validator (required by a live gate)

`reference_join` declares `key_field` as an arriving column, so it overrides
`TransformDataConfig.declared_input_fields` and is therefore **discovered
automatically** by `tests/invariants/test_input_options_do_not_name_created_fields.py`,
whose roster is swept from a live `PluginManager`. That gate will mutate
`key_field` to name one of our created fields and require the config to be
REFUSED with an error naming the option to repoint.

So the config model must reject `key_field ∈ output.keys()` at construction,
with a message naming `key_field`. Rejection is asserted behaviourally, so
either the shared helper or a native validator satisfies it.

Related naming constraint: `is_column_naming_config_option`
(`plugins/infrastructure/base.py:82-91`) matches `field`, `fields`, `group_by`
and any `*_field`/`*_fields` suffix. `reference_key_name` must NOT be named
`reference_key_field` — it names a column in the reference TABLE, not on the
row, and the `_field` suffix would falsely enrol it as a row-column option.

## Limits the user must accept before this is built

**The web path has a hard 256 KiB ceiling per reference table**
(`BLOB_INLINE_PER_REF_BYTE_CAP`, `core/blobs_inline.py:65`), with 1 MiB
aggregate across all inline refs in a pipeline
(`BLOB_INLINE_AGGREGATE_BYTE_CAP`, `:66`). A real product-definitions table may
exceed this. This is a functional ceiling on the web surface, not a detail —
if production tables are larger, the web half needs a different mechanism and
that is a change to this design, not a tuning exercise.

**Web blob bytes are mutable in place.** Web blob ids are uuid4 handles, not
content hashes, and `update_blob` (`web/composer/tools/blobs.py:1534-1578`)
overwrites bytes under the same id with no status filter; only pending/running
runs are guarded. The `inline_content` sha256 verification is what protects a
given composition, since the hash is captured at bind time — but "the blob id
is stable" must not be read as "the bytes are".

**On the CLI, the reference file must live inside the config directory.**
`_resolve_template_path` blocks traversal outright, so `products.csv` sits next
to `settings.yaml`.

## Scope

**In scope, v1:** CLI and web. The web half was accepted deliberately after the
extra design cost was surfaced.

**Out of scope:** the side-input edge from a declared source (rejected above);
any second plugin splitting CSV from JSON; multi-key / composite-key joins;
range or fuzzy matching; and reference tables large enough to need streaming or
an index.

## Delivery surface

**Authorization is operator config, not a code change.** `authorized =
REQUIRED_WEB_PLUGIN_IDS | optional` (`web/plugin_policy/compiler.py:106`), and
`optional` comes from `settings.plugin_allowlist` (`web/config.py:442`).
`REQUIRED_WEB_PLUGIN_IDS` (13 entries, `compiler.py:17-39`) is a floor, not the
gate; the REQUIRED route additionally hand-mirrors into two Terraform files
pinned by a test and should be avoided.

**Composer parity is small.** Planner skills name zero transform plugins — they
route to live discovery. The frontend needs zero changes: plugin ids are a
regex, not a union; one renderer; option forms are server-schema-driven. The
guided lane dispatches on node KIND, never plugin name. Per-plugin facts go on
the class via `get_agent_assistance`, not into skills.

**The large surface is CI gates, which `PLUGIN.md:829-843` does not list:**
golden knob schema (no record mode), the contracts whitelist in two places
(trailing segment must match the actual param name),
`EXPECTED_TRANSFORM_DETERMINISMS` (`web/audit_readiness/boundary_expectations.py:194`
— production source, not a test fixture), roughly six count bumps, and
`evidence_selectors.json` (needs five lane-level `node_ids` per plugin plus
~40 cell entries, and NOTHING gates the `node_ids` — `validate-selectors`
returns 0 either way, so diff against a sibling plugin).

`source_file_hash` has its own trap: seed a real `"sha256:..."`-shaped literal
before computing, because the normaliser regex only matches that shape and a
`None` placeholder is hashed as content, leaving the first value stale. Iterate
to a fixed point, compute AFTER `ruff format`, and assert strict equality — a
substring assertion passes on a comment. No local test enforces this; a stale
hash once passed 10,213 tests.

The v2 state-engine rotation is ordered: `CANONICAL_V2_LEGS_SHA256`, then
`CANONICAL_V2_EXECUTION_PROFILES_SHA256` (both via the validator's own
`_semantic_sha256`), then `V2_CATALOG_SHA256` last. `first_party_plugins` lives
under `execution_profiles`, which is why that fingerprint moves.

**Add the `EmittedToOutput` marker on the base class** if any option value can
reach output. The env-placeholder hand-map is gone (627fe8b7c), replaced by
`Annotated` metadata (`contracts/emitted_option.py`); a redeclaring subclass
silently drops inherited `Annotated` metadata (`emitted_option.py:50-58`).

## Sequencing

`blob_json_expand` is in flight in worktree `.claude/worktrees/blob-expanders`
on `feature/blob-expanders`, and that lane is deliberately NOT sweeping the
whole-tree count gates — it lands the plugin and unit tests knowingly red on
the count pins. No file collision with this work, but **whoever sweeps second
inherits the combined count**, so coordinate before sweeping.

That branch also carries `plugins/transforms/blob_expand_contract.py`
(`DEFAULT_BLOB_REF_FIELD` and siblings). It declares no class, so discovery
ignores it. Consume it rather than repeating literals — but it is UNMERGED, so
importing it from `feature/unified-lineage` today raises `ImportError`, and
three agents already import it, so ask before adding a constant.

## Already red at HEAD — do not self-attribute

* `tests/invariants/test_input_options_do_not_name_created_fields.py`
  (`llm.output_fields`).
* `test_planner_authoring_aids.py` `canonical_utf8_budget`: 50,365 B against a
  48 KiB cap, filed `elspeth-623c69c59f`. Directly relevant here — the
  whole-catalog planner prose budget is ALREADY negative, so
  `reference_join`'s `summary` and `usage_when_not_to_use` text will get no
  clean signal from that test in either direction.

## Testing

* **Join semantics:** CSV flat hit; JSON nested hit via `ref['tax']['rate']`;
  multiple outputs from one match.
* **Miss policy:** each of `fail` / `null` / `default`, for a key miss AND for
  an unresolvable path inside a matched entry — the disambiguation above is
  asserted, not assumed.
* **Duplicate keys:** rejected at config load, error names the key and the
  positions.
* **`key_field` collision:** rejected at construction, error names `key_field`.
  The live invariant gate exercises this, but **that gate is already RED at
  HEAD** (`llm.output_fields`), so "the gate passes" cannot be the evidence.
  Verification is a targeted assertion plus a before/after failure-set DIFF —
  the gate's finding set must lose nothing and gain nothing but what we
  intended.
* **`probe_config()`:** the same gate's anti-vacuity clause requires every
  roster member to construct under its own `probe_config` and to expose at
  least one scalar `*_field` option the gate can repoint. `key_field` satisfies
  the second; `probe_config()` must exist and construct cleanly, or
  `reference_join` is reported as uncovered rather than counted as passing.
* **`default_values` keys:** a key absent from `output` is rejected at
  construction.
* **Both delivery paths agree:** the same table via `reference_file` and via
  `inline_content` substitution produces identical rows — the regression test
  for the type-mismatch bug this design exists to avoid.
* **Reproducibility:** mutating the reference file changes the topology hash
  and resume is refused. Mutation-test it: revert the change and confirm resume
  is ACCEPTED, so the test cannot pass by refusing everything.
* **Malformed table:** unparseable CSV and unparseable JSON both raise
  `PluginConfigError` at load, not at row time.
