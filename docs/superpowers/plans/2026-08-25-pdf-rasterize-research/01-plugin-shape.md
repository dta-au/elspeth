# Research lane 1 — plugin shape (sibling exploders, payload store, contracts)

Read-only sweep of the worktree at `a371e13d0` by an Explore agent, 2026-08-25.
Line numbers are as-of that commit. Raw research input for
`../2026-08-25-pdf-rasterize.md`; decisions taken from it are recorded in the
plan, not here.

## 1. Sibling exploder structure

`line_explode.py` (508 lines) and `blob_csv_expand.py` (505 lines) share one layout:
module docstring → module-level ceiling constants → pydantic config model → free
functions building the output `SchemaConfig` → the `BaseTransform` subclass.

Module-level ceilings: `line_explode.py:38-40` (`DEFAULT_MAX_LINES`,
`HARD_MAX_LINES`), applied at `:50-55` as `Field(default=..., gt=0, le=HARD_MAX_LINES)`.
`aws/textract_inline_analysis.py:158-163` is the "may be reduced, never raised" shape:
`Field(default=BINARY_DOCUMENT_MAX_BYTES, gt=0, le=BINARY_DOCUMENT_MAX_BYTES)`.

Validators to copy: `line_explode.py:57-81` (empty names, `.isidentifier()`,
collisions between every emitted field and the source field); `blob_csv_expand.py:97-99`
(`declared_input_fields` property widening `super()` by the input field name).

### Class attributes (`line_explode.py:291-322`, `blob_csv_expand.py:154-193`)

| Attribute | line_explode | blob_csv_expand |
|---|---|---|
| `determinism` | `DETERMINISTIC` | `IO_READ` (reads payload store) |
| `plugin_version` | `"1.0.0"` (never `"0.0.0"` — lint PH1) | same |
| `source_file_hash` | `"sha256:<16 hex>"` | same |
| `creates_tokens` | `True` (:321) | `True` (:185) |
| `passes_through_input` | not set (uses `forwards_input_fields`) | `True` (:186) |
| `output_naming_config_keys` | `{"output_field","index_field"}` | `{"row_index_field"}` — every option naming an EMITTED field, never the input locator |
| `capability_tags` | 4 tags | `("csv","blob","tabular","fan-out")` — 2–6 unique lowercase-kebab, not a subset of `{general,generic,plugin,utility}` (`tests/fixtures/catalog_reference.py:30-31,88-98`) |
| `usage_when_to_use` / `usage_when_not_to_use` / `example_use` | :301-319 | :165-183 — all non-blank, mutually distinct, globally unique; `example_use` = parseable YAML declaring the plugin exactly once under `transform:` |
| `probe_config()` | :323-329 | :188-193 — mandatory (`tests/invariants/test_transform_probe_coverage.py`) |
| `get_agent_assistance()` | :373-423 | :217-231 — `composer_hints` tuple inside `PluginAssistance` |

### Multi-row emission

`blob_csv_expand.py:375-385`: build list of dicts, heterogeneous-key guard
(`:362-369`, raise `ValueError` if keys differ), derive ONE contract, wrap every row
in `PipelineRow(r, output_contract)` reusing the same object. The contract triple:

```python
output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output_rows[0])
output_contract = self._apply_declared_output_field_contracts(output_contract)
output_contract = self._align_output_contract(output_contract)
```

`TransformResult.success_multi` (`contracts/results.py:393-435`): non-empty rows
(`:423-424`), every element a `PipelineRow`, **`rows[i].contract is rows[0].contract`
by identity** (`:54-70`). `success_reason` shape used by blob_csv_expand:
`{"action": "expanded_blob", "fields_added": [...], "metadata": {...}}`;
`"expanded_blob"` is in the closed `TransformActionCategory` (`contracts/errors.py:329-355`).

### Typed row errors

`TransformResult.error(reason, *, retryable=False)`; `reason["reason"]` is typed
`TransformErrorCategory`, a closed `Literal` at `contracts/errors.py:489-605`; mypy is
the enforcement. Reusable: `blob_not_found`, `blob_too_large`, `invalid_input`,
`missing_field`, `too_many_rows`, `validation_failed`, `decode_failed`. Absent and
needed: encrypted PDF, malformed PDF, page render failure, rendered page too large,
render timeout. `IntegrityError` from the payload store is RE-RAISED
(`blob_csv_expand.py:307-308`, `textract_inline_analysis.py:452-458`) — infrastructure
fault, never a row error.

### Expand-group minting is engine-side

Plugin sets `creates_tokens = True` and returns `success_multi`; nothing else.
`engine/token_traversal.py:240-247` guards multi-row from `creates_tokens=False`;
`:198-231` a zero-row `success_empty()` from a `creates_tokens=True` transform mints a
durable `group_records` row with `member_count=0`. `engine/tokens.py:483-586`
`TokenManager.expand_token` requires `output_contract.locked` (`:525-529`) —
`narrow_contract_to_output` always returns `locked=True`.

### `source_file_hash`

`scripts/cicd/plugin_hash.py:84-105` `compute_source_file_hash(path)`;
`fix_source_file_hash(path, class_name, hash)` rewrites in place. Never hand-edit;
run AFTER `ruff format`, compare by strict equality.

## 2. Reading binary input

Reference = 64-lowercase-hex SHA-256 as a plain `str` field. Use
`re.compile(r"[0-9a-f]{64}\Z").fullmatch` (`textract_inline_analysis.py:68`, `:468`).

Payload store (`core/payload_store.py:43-319`): `store(content: bytes) -> str`
(`:210-262`, content-addressed, atomic), `retrieve(hash) -> bytes` (`:264-288`, raises
`PayloadNotFoundError` / `IntegrityError`). Obtain it in `on_start`
(`blob_csv_expand.py:269-273`): `if ctx.payload_store is None: raise FrameworkBugError(...)`.

`contracts/binary_documents.py`: `BINARY_DOCUMENT_MAX_BYTES = 5 * 1024 * 1024`
(`:27-33`); `binary_document_signature_matches("pdf", data)` (`:76-87`) — call it before
handing bytes to the renderer (mirrors `textract_inline_analysis.py:498-507`).

textract's resolve-and-validate order (`textract_inline_analysis.py:452-508`), copy it:
missing_field → non-str invalid_input → not-64-hex invalid_input → store None
FrameworkBugError → PayloadNotFoundError blob_not_found (IntegrityError propagates) →
empty invalid_input → too large blob_too_large → signature mismatch invalid_input.

`blob_rows` emits exactly five fixed fields (`plugins/sources/blob_rows.py:224-232`):
`blob_id` (UUID), `blob_ref` (64-hex), `blob_filename`, `blob_mime_type`,
`blob_size_bytes`. Default `blob_ref_field: "blob_ref"` matches.

## 3. Writing rows a downstream `aws_textract_inline_analysis` consumes

It reads exactly ONE row field — its `blob_ref_field` (default `blob_ref`) — whose
bytes must start with the signature of its configured `document_format` (`png`).

**Naming collision gate**: `plugins/infrastructure/base.py:957-1000`
`_reject_input_options_naming_created_fields` raises `PluginConfigError` when an
input-locator option names a field the transform creates
(`tests/invariants/test_input_options_do_not_name_created_fields.py`). So the page
image ref must go in a DIFFERENT field (`page_blob_ref`), listed in
`output_naming_config_keys`, and the reject call goes at the END of `__init__` after
`declared_output_fields` is populated (`base.py:973-976`; precedent
`textract_inline_analysis.py:341`). Downstream config therefore needs
`blob_ref_field: page_blob_ref` and `document_format: png`.

The 5 MiB ceiling propagates backwards: the rasterizer needs a bounded DPI and a
per-page rendered-byte ceiling defaulting at/below `BINARY_DOCUMENT_MAX_BYTES`.

`Pages != 1` guard (`:556-565`) validates the provider RESPONSE; stays unchanged.

## 4. Schema / contract mechanics

`SchemaConfig` (`contracts/schema.py:425+`): `mode` lowercase at YAML level; runtime
`SchemaContract.mode` is uppercase. Expand-transform output-schema builder pattern
(`blob_csv_expand.py:125-151`): copy input fields, overlay added `FieldDefinition`s,
union `guaranteed_fields`, `mode = cfg.schema_config.mode if fields is not None else "flexible"`.
Assign to `self._output_schema_config`.

`SchemaConfigModeViolation` (`engine/executors/schema_config_mode.py:150-232`) is a
runtime post-emission cross-check of each row's contract against
`_output_schema_config`; the contract triple keeps them in agreement. Build-time
counterpart `core/dag/builder.py:126-143`.

`passes_through_input` is runtime-cross-checked as a PRESENCE contract
(`engine/executors/pass_through.py:60-129`, TIER_1 `PassThroughContractViolation`).
`forwards_input_fields` + `removed_input_fields` is static-only. If every input field
survives on every page row → `passes_through_input = True` (blob_csv_expand shape,
forward probe). If the design drops a field → must use `forwards_input_fields`.

`PipelineRow` (`contracts/schema_contract.py:497-670`): `type(data) is dict` exactly;
frozen; `.to_dict()` mutable deep copy. `guaranteed_fields` must hold every field
unconditionally emitted.

`BaseTransform` helpers: `_initialize_declared_input_fields(cfg)` (`base.py:630`),
`create_schema_from_config` twice by hand (`blob_csv_expand.py:213-215`),
`_augment_invariant_probe_row` (`base.py:895`).

## 5. Trust-tier / ADR-032 / masquerade

`@trust_boundary` (`contracts/trust_boundary.py`) suppresses R1/R5 inside a function
that reads external data with `.get()`/`isinstance`; a renderer handing bytes to a
library and getting bytes back does neither, so do NOT decorate it (spec §4 Low
wardline row: it would become ERROR #7). Parse structured metadata from the parsed PDF
object per ADR-032 (construct an owned type, then direct attribute access).

Masquerade gate (`elspeth-lints .../masquerade/`, `RuleScope.WHOLE_REPO`, scan roots
`src/elspeth, tests, scripts, elspeth-lints/src`): **zero `getattr`/`hasattr` in the
plugin AND its tests.** Sanctioned probe-swap pattern: `self.__dict__["_payload_store"]`
+ `delattr` (`blob_csv_expand.py:256-267`).

## 6. Test patterns

Closest structural match: `tests/unit/plugins/transforms/test_blob_csv_expand.py`.
Richest: `test_json_explode.py`. Rows via `elspeth.testing.make_pipeline_row({...})`;
ctx via `tests/fixtures/factories.py:120` `make_context()`. Assert
`result.status == "success"`, `result.is_multi_row`, `[r.to_dict() for r in result.rows]`;
errors via `result.reason["reason"]` and `result.retryable`. End each file with the
registration smoke test (`PluginManager()` → `register_builtin_plugins()` →
`get_transform_by_name`).

Payload-store fixtures: `tests/fixtures/stores.py:17-49,73-76` (`MockPayloadStore`,
`payload_store` fixture); `FilesystemPayloadStore(tmp_path / "payloads")` where real
hashing matters.

End-to-end expand-group template: `tests/integration/pipeline/test_deaggregation.py`
— programmatic path `:193-260` (`load_settings` → `instantiate_plugins_from_config` →
`ExecutionGraph.from_plugin_instances` → `PipelineConfig` → `Orchestrator(db).run(...,
payload_store=...)`); group assertions `:264-317` derive `expand_group_id` from
`lineage_path` via `elspeth.contracts.identity.path_expand_group_id` — never a stored
column read.

Invariant probe (`blob_csv_expand.py:233-267`): override
`forward_invariant_probe_rows` + `execute_forward_invariant_probe`, swap a hermetic
store via `__dict__`. For pdf_rasterize the render seam is also swapped in the probe
(textract stubs its SDK the same way, `textract_inline_analysis.py:643-659`).

Semantics declarations: declare neither `input_semantic_requirements` nor
`output_semantics` for v1 (blob_csv_expand precedent; `line_explode.py:184-199` records
why a FAIL requirement blocked legitimate compositions).
