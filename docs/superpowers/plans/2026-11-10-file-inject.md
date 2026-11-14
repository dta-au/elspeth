# `file_inject` Transform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `file_inject` transform that writes the whole content of config-bound files into row fields, format-aware, in `select` and `all` modes, usable from YAML and the Web Composer.

**Architecture:** Content is bound as configuration through the existing file-option registry (`content_file` → `content`), so it is part of node identity. The config model parses every entry once at validation; `process` only copies. On the web, only user-uploaded blobs may back `content`, enforced by extending the existing LLM-authored-blob predicate that authoring, `/validate` and run admission already share.

**Tech Stack:** Python 3.13, pydantic v2, pytest (`addopts` carries `-n 12`; use `-n 0` for focused runs), ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-10-file-inject-design.md`

## Global Constraints

- Work only in the worktree `.claude/worktrees/new-plugin` on branch `claude/happy-cray-mbfmm1`. Run tools as `.venv/bin/python -m pytest …` from the worktree root.
- Read CONTRIBUTING.md § "Whole-tree gates and conventions you will hit" before writing code. No `getattr`/`hasattr` probes on owned types; no `# noqa`, `# type: ignore` or lint suppressions; no bare `raise TypeError` reachable from `process`.
- Plugin name `file_inject`; class `FileInject`; `determinism = Determinism.DETERMINISTIC`; `plugin_version = "1.0.0"`; `passes_through_input = True`.
- Formats: exactly `text`, `json`, `jsonl`, `csv`. `format` and `mode` are required, no defaults, no inference.
- Caps: per entry ≤ 16 MiB (`16 * 1024 * 1024`) canonical size; per node ≤ 64 MiB (`64 * 1024 * 1024`).
- `select` matching is exact string equality; a non-`str` key is a row error.
- Composer invariants: the guard only validates and rejects planner output. No server-authored structure, no tutorial-specific path.
- Never hand-edit a `judge_metadata_signature` or a fingerprint. Trust-tier findings from new code are left for the operator; record the before/after finding sets only.
- Every commit: run `scripts/branch-safety-check.sh --intent commit` first; end the message with the session's attribution trailers. Task 5's commit also needs the trailer `telemetry-backfill: audit-readiness`.
- Do not add a changelog or release-notes entry.

## Review Focus

1. **A file with a UTF-8 byte-order mark** (common from Windows editors). Expected: JSON/JSONL/CSV parse normally and the first CSV header is not polluted; `text` is written as-is. Pinned in Task 3.
2. **YAML entry names that are not strings** (`1:`, `true:`, `no:` under `files`). Expected: a validation error naming the entry and saying entry names must be quoted strings, not a silent non-match at run time. Pinned in Task 3.
3. **An empty or whitespace-only file.** Expected: `text` writes the empty string; `json` and `csv` are refused at validation with the entry named; `jsonl` yields `[]`. Pinned in Task 3.
4. **Key values that differ only by case or surrounding whitespace** (`" A"`, `"a"`). Expected: treated as a miss under `on_miss`, and the row error lists the sorted entry names so the author can see why. Pinned in Task 4.
5. **CRLF line endings** in CSV and JSONL. Expected: no `\r` left in the last cell or line value. Pinned in Task 3.

## File Structure

| File | Responsibility |
|---|---|
| Create `src/elspeth/plugins/infrastructure/config_content.py` | Shared strict-parsing helpers for config-bound content (moved from `reference_join` and `json_source`). |
| Create `src/elspeth/plugins/transforms/file_inject.py` | Config models, entry parsing, the `FileInject` transform. |
| Modify `src/elspeth/core/template_materialization.py` | `content_file` registry entry; nested-container resolution declared on the registry. |
| Modify `src/elspeth/contracts/errors.py` | One new error reason. |
| Modify `src/elspeth/web/execution/_validation_materialization.py`, `src/elspeth/web/execution/service.py`, `src/elspeth/web/composer/tools/blobs.py` | Upload-only guard. |
| Modify `src/elspeth/web/plugin_policy/compiler.py` | Default authorized set. |
| Modify registration and pin files listed in Task 5 | Inventory pins. |
| Create `examples/file_inject/` | One `all` and one `select` example. |

---

### Task 1: Shared config-content parsing helpers

**Files:**
- Create: `src/elspeth/plugins/infrastructure/config_content.py`
- Modify: `src/elspeth/plugins/transforms/reference_join.py` (remove `_bounded_canonical_size` at ~79 and `_reject_duplicate_json_object_members` at ~261; import instead)
- Modify: `src/elspeth/plugins/sources/json_source.py` (remove `_reject_nonfinite_constant` at ~42; import instead)
- Test: `tests/unit/plugins/infrastructure/test_config_content.py`

**Interfaces:**
- Produces, in `config_content.py`, with bodies moved verbatim:
  - `bounded_canonical_size(value: object, *, limit: int) -> int`
  - `reject_duplicate_json_object_members(pairs: list[tuple[str, object]]) -> dict[str, object]`
  - `reject_nonfinite_constant(value: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
def test_duplicate_members_are_refused():
    with pytest.raises(ValueError):
        json.loads('{"a": 1, "a": 2}', object_pairs_hook=reject_duplicate_json_object_members)

def test_nonfinite_constant_is_refused():
    with pytest.raises(ValueError):
        json.loads("NaN", parse_constant=reject_nonfinite_constant)

def test_bounded_size_stops_at_limit():
    assert bounded_canonical_size("x" * 100, limit=10) > 10
    assert bounded_canonical_size({"a": 1}, limit=1000) == len('{"a":1}')
```

Before writing these, read the three moved bodies and adjust the exception type and the size expectation to what they actually raise and return; the assertions must describe current behaviour, not change it.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/plugins/infrastructure/test_config_content.py -n 0` — expect FAIL (module missing).
- [ ] **Step 3: Move the three functions** into the new module under the public names above; update every reference in `reference_join.py` and `json_source.py`. No behaviour change.
- [ ] **Step 4: Run** the new test plus `tests/unit/plugins/transforms/test_reference_join.py tests/unit/plugins/transforms/test_reference_join_multi.py tests/unit/plugins/sources -n 0` — expect PASS.
- [ ] **Step 5: Re-pin both edited plugins' `source_file_hash`** after `ruff format`, using the command in CONTRIBUTING.md § "Gate: plugin inventories…", then follow its dependency order for the scenario-corpus manifest. Run `.venv/bin/python -m pytest tests/integration/core/dag tests/unit/architecture` — expect PASS.
- [ ] **Step 6: Commit** — `refactor(plugins): share strict config-content parsing helpers`.

---

### Task 2: Loader — `content_file` and declared nested containers

**Files:**
- Modify: `src/elspeth/core/template_materialization.py`
- Test: `tests/unit/core/test_file_inject_materialization.py` (new); `tests/unit/core/test_reference_table_materialization.py` must pass unmodified.

**Interfaces:**
- Produces: `FileBackedTemplateOption` gains `nested_in: tuple[str, str] | None = None` — `(plugin_name, container_option_key)` — and `top_level: bool = True`.
  - `reference_file` entry: `nested_in=("reference_join", "tables")`, `top_level=True`.
  - New entry: `file_key="content_file"`, `content_key="content"`, `source_key="content_source"`, `label="Injected file"`, `content_kind="text"`, `nested_in=("file_inject", "files")`, `top_level=False`.
- `materialize_options` skips rules with `top_level=False`. `_materialize_plugin_config` and `reject_file_backed_options` iterate rules with `nested_in` instead of testing `plugin_name == "reference_join"`. The per-entry error prefix for `reference_join` stays byte-identical (`Reference table {name!r}: …`); for `file_inject` it is `Injected file {name!r}: …`.

- [ ] **Step 1: Write the failing tests** (use `tmp_path` and `TemplateOptionMaterializer(settings_path)`, following the fixtures in `test_reference_table_materialization.py`):

```python
def test_content_file_becomes_content_and_source(tmp_path): ...
    # files.A == {"content": "hello\n", "content_source": "a.md", "format": "text"}; no "content_file" key
def test_content_file_outside_settings_dir_is_refused(tmp_path): ...
    # TemplateFileError, message starts "Injected file 'A': "
def test_content_and_content_file_together_are_refused(tmp_path): ...
def test_top_level_content_file_is_not_materialized(tmp_path): ...
    # options {"content_file": "a.md"} on a file_inject node are returned unchanged
def test_replay_uses_recorded_content_without_reading(tmp_path): ...
    # run_mode=RunMode.REPLAY, file deleted, source_settings carries content+content_source
def test_in_memory_loading_rejects_nested_content_file(): ...
    # reject_file_backed_options raises ValueError naming "files['A'].content_file"
def test_other_plugins_files_option_is_untouched(tmp_path): ...
    # a non-file_inject plugin with options.files.A.content_file is returned unchanged
```

- [ ] **Step 2: Run** the new file with `-n 0` — expect FAIL.
- [ ] **Step 3: Implement** the two dataclass fields, the registry entry, and the generalised nested walk.
- [ ] **Step 4: Run** `tests/unit/core/test_file_inject_materialization.py tests/unit/core/test_reference_table_materialization.py tests/unit/core/test_config_loading.py tests/unit/core/test_replay_local_io.py -n 0` — expect PASS with the existing files unedited.
- [ ] **Step 5: Commit** — `feat(core): bind file_inject content through the file-option registry`.

---

### Task 3: Config models and entry parsing

**Files:**
- Create: `src/elspeth/plugins/transforms/file_inject.py` (config half)
- Test: `tests/unit/plugins/transforms/test_file_inject_config.py`

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces:
  - `MAX_FILE_INJECT_ENTRY_BYTES = 16 * 1024 * 1024`, `MAX_FILE_INJECT_NODE_BYTES = 64 * 1024 * 1024`
  - `parse_injected_content(content: str, *, fmt: Literal["text","json","jsonl","csv"], data_key: str | None, delimiter: str) -> object` — raises `ValueError`.
  - `class FileInjectEntryConfig(BaseModel)` — `extra="forbid"`, frozen; fields `content: Annotated[str, EmittedToOutput(..., reject_at_validation=False)]`, `content_source: str | None = None`, `format: Literal["text","json","jsonl","csv"]`, `data_key: str | None = None`, `delimiter: str = ","`, `output_field: str | None = None`.
  - `class FileInjectConfig(TransformDataConfig)` — fields `mode: Literal["select","all"]`, `files: dict[str, FileInjectEntryConfig]`, `key_field: str | None = None`, `output_field: str | None = None`, `on_miss: Literal["fail","null","default"] | None = None`, `default_content: Any = None`; private `_parsed: dict[str, object]` (entry name → parsed value) set by an `after` model validator. Effective `on_miss` in `select` is `"fail"` when unset.
  - `added_output_fields(cfg: FileInjectConfig) -> tuple[FieldDefinition, ...]` — the declared types from the spec's "Declared output types".

Parsing decisions the tests pin:
- A leading U+FEFF is stripped before parsing `json`, `jsonl` and `csv`; `text` is never modified.
- `jsonl` and `csv` split and read with universal newlines; blank `jsonl` lines are skipped.
- `csv` uses `csv.DictReader(..., strict=True)`; missing header, empty or duplicate header names, and a row with more or fewer cells than the header are refused.
- `data_key` requires a top-level JSON object that contains the key.
- `delimiter` must be exactly one character.
- Entry names must be `str`; a validation error for a non-string name says entry names must be quoted strings.

- [ ] **Step 1: Write the failing tests.** One test per row, named for the behaviour:

```python
# values
test_text_is_written_unmodified          # "a\n\n" -> "a\n\n"; BOM kept
test_json_object_array_and_scalar        # '{"a":[1,2.5,null]}' -> {"a":[1,2.5,None]}; "3" -> 3
test_json_data_key_takes_member          # data_key="rubric"
test_jsonl_one_value_per_nonblank_line   # '{"a":1}\n\n[2]\n' -> [{"a":1},[2]]
test_csv_rows_keyed_by_header            # "k,v\na,1\n" -> [{"k":"a","v":"1"}]; cells are str
test_csv_custom_delimiter                # delimiter=";"
# review focus 1, 3, 5
test_bom_is_stripped_for_structured_formats   # "﻿k,v\na,1\n" -> first header is "k"
test_empty_file_per_format               # text -> ""; jsonl -> []; json and csv -> ValidationError naming the entry
test_crlf_leaves_no_carriage_return      # "k,v\r\na,1\r\n" -> [{"k":"a","v":"1"}]; jsonl likewise
# refusals (each asserts ValidationError and that the message names the entry or option)
test_malformed_json_refused
test_nan_and_duplicate_members_refused
test_data_key_on_non_object_or_absent_refused
test_csv_ragged_row_and_bad_headers_refused
test_format_option_on_wrong_format_refused        # data_key on csv; delimiter != "," on json
test_entry_needs_content
test_duplicate_output_field_refused               # all mode
test_output_field_colliding_with_declared_input_refused
test_select_requires_key_field_and_output_field
test_all_requires_per_entry_output_field
test_wrong_mode_options_refused                   # on_miss/key_field/top-level output_field in all; entry output_field in select
test_default_content_iff_on_miss_default
test_non_string_entry_name_refused                # review focus 2: {1: {...}} and {True: {...}}
test_entry_and_node_caps                          # monkeypatch the two constants small
# declared types
test_all_mode_types                # text -> "str"; json/jsonl/csv -> "any"
test_select_types                  # all text + fail -> "str"; any non-text -> "any"; on_miss null -> nullable;
                                   # on_miss default with non-str default_content -> "any"
```

- [ ] **Step 2: Run** `tests/unit/plugins/transforms/test_file_inject_config.py -n 0` — expect FAIL.
- [ ] **Step 3: Implement** the constants, `parse_injected_content`, the two models with their validators, and `added_output_fields`.
- [ ] **Step 4: Run** the same file — expect PASS.
- [ ] **Step 5: Commit** — `feat(file_inject): config models and format-aware parsing`.

---

### Task 4: The transform

**Files:**
- Modify: `src/elspeth/plugins/transforms/file_inject.py` (transform half)
- Modify: `src/elspeth/contracts/errors.py` (add `"file_inject_miss"` to the reason literal after `"reference_miss"`, with a comment)
- Test: `tests/unit/plugins/transforms/test_file_inject.py`

**Interfaces:**
- Consumes: Task 3's `FileInjectConfig`, `added_output_fields`.
- Produces: `class FileInject(BaseTransform)` with `name = "file_inject"`, `config_model = FileInjectConfig`, `capability_tags = ("file", "enrichment", "prompt-context", "reference-data")`, `output_naming_config_keys = frozenset({"output_field"})` (confirm against `is_column_naming_config_option` and how other plugins with an `output_field` option classify it), `usage_when_to_use`, `usage_when_not_to_use`, `example_use`, `probe_config()`, `get_agent_assistance()`, `process(row, ctx) -> TransformResult`, `close()`.
- `__init__` builds input and output schemas the way `ReferenceJoin.__init__` does, using `added_output_fields`.

Row behaviour (`select`), as error dicts:

| Situation | Result |
|---|---|
| `key_field` not in row | `{"reason": "missing_field", "field": key_field, "error": …}`, `retryable=False` |
| key value not `str` | `{"reason": "type_mismatch", "field": key_field, "error": …}` |
| miss, `on_miss: fail` | `{"reason": "file_inject_miss", "field": key_field, "error": f"no file_inject entry named for the row's {key_field!r}; entries: {sorted_names}"}` |
| miss, `null` / `default` | writes `None` / `default_content` |
| an output field already present on the row (observed input) | `{"reason": "invalid_input", "field": <name>, "error": …}` in both modes |

Error dicts never contain file content. Before using `missing_field`, `type_mismatch` and `invalid_input`, check each one's required companion keys in `TransformErrorReason`.

Success: `TransformResult.success(PipelineRow(output, contract), success_reason={"action": "enriched", "fields_added": sorted(...), "metadata": {...}})` where metadata is `{"mode": "all"}` or `{"mode": "select", "matched": bool, "on_miss": ...}`. Non-string values are `copy.deepcopy`'d per row. The contract is derived as in `ReferenceJoin.process`.

`get_agent_assistance` composer hints must include, in substance: the two modes; `format` is stated per entry and never inferred; wire an uploaded blob with `wire_blob_inline_ref` at `node:<node_id>.options.files.<entry_name>.content`; on the CLI use `content_file` under each entry; only user-uploaded files can be attached, so when the user wants text they have not uploaded, tell them to save it as a file and upload it, then attach the upload, and do not use `create_blob` as a substitute. Avoid credential-shaped phrasing (`token:` and similar).

- [ ] **Step 1: Write the failing tests:**

```python
test_all_mode_writes_every_field
test_select_writes_matching_entry
test_select_miss_fail_is_row_error_listing_entries     # reason "file_inject_miss"; error text contains "['A', 'B']"; no content in the dict
test_select_miss_null_and_default
test_select_case_and_whitespace_are_misses             # review focus 4: " A" and "a" against entry "A"
test_select_missing_key_field_is_error_regardless_of_on_miss
test_select_non_string_key_is_error_regardless_of_on_miss   # 1, None, True
test_output_field_already_on_row_is_row_error          # observed schema
test_rows_do_not_share_mutable_values                  # mutate row 1's list; row 2 unchanged
test_output_schema_declares_and_guarantees_added_fields
test_probe_config_constructs
```

- [ ] **Step 2: Run** `tests/unit/plugins/transforms/test_file_inject.py -n 0` — expect FAIL.
- [ ] **Step 3: Implement** the class and the error reason.
- [ ] **Step 4: Run** both `test_file_inject*.py` files and `tests/unit/plugins/test_process_path_type_error_gate.py -n 0` — expect PASS.
- [ ] **Step 5: Commit** — `feat(file_inject): select and all transform`.

---

### Task 5: Registration and inventory pins

**Files** (each is an exact pin; follow CONTRIBUTING.md § "New plugin checklist" item by item):
- `src/elspeth/plugins/infrastructure/run_mode_capabilities.py` — add `"file_inject": ("elspeth.plugins.transforms.file_inject", "FileInject")` in sorted position.
- `src/elspeth/web/audit_readiness/boundary_expectations.py` — `"file_inject": Determinism.DETERMINISTIC` with the `reference_join` reasoning.
- `tests/unit/plugins/test_discovery.py` — `EXPECTED_TRANSFORM_COUNT` + 1.
- `tests/unit/plugins/test_catalog_reference_content.py` — totals, per-kind counter, `EXPECTED_BUILTIN_IDENTITIES`, `DIRECT_CONFIG_REFERENCES`.
- `tests/unit/plugins/transforms/test_core_catalogue_metadata.py` — add `file_inject` wherever `reference_join` is listed.
- `tests/unit/plugins/test_validation_path_agreement.py` — a `_TRANSFORM_REJECTION_CASES` entry (use `mode: all` with a missing per-entry `output_field`).
- `tests/invariants/test_input_schema_config_is_captured.py`, `tests/invariants/test_transform_input_contract_is_satisfiable.py` — add only if the live registry reports a rejection.
- `tests/unit/web/catalog/test_service.py` — serialized-summary total; generate `tests/golden/web/catalog/knob_schema/transform__file_inject.json` from `CatalogServiceImpl._schema_cache`.
- `config/cicd/contracts-whitelist.yaml` — `probe_config:return` and constructor entries.
- State-engine lifecycle matrix: `scripts/state_engine_plugin_matrix.py`, `tests/golden/state_engine/plugin_lifecycle_matrix.json`, `tests/unit/plugins/test_state_engine_plugin_matrix.py`, PB-09 cases in `docs/architecture/state_engine/proof-catalog/v2` and `v3`, then the hash pins in the order the checklist gives. Derive the five matrix fields from `reference_join`'s rows field by field.
- `src/elspeth/plugins/transforms/file_inject.py` — pin `source_file_hash` **last**.

- [ ] **Step 1: Run the pin tests to see them fail:** `.venv/bin/python -m pytest tests/unit/plugins/test_discovery.py tests/unit/plugins/test_catalog_reference_content.py tests/unit/web/catalog/test_service.py tests/unit/plugins/test_state_engine_plugin_matrix.py -n 0 > .claude/lanes/task5-red.log 2>&1; echo "exit=$?"` — expect non-zero, with each failure naming `file_inject` or a count off by one.
- [ ] **Step 2: Update each pin** from the live registry or its generator. Do not type a count from inference.
- [ ] **Step 3: Pin `source_file_hash`** after `ruff format`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/unit/plugins tests/invariants tests/unit/web/catalog tests/integration/core/dag tests/unit/architecture tests/integration/plugins > .claude/lanes/task5-green.log 2>&1; echo "exit=$?"` — expect `exit=0`.
- [ ] **Step 5: Commit** — `feat(file_inject): register the plugin and move inventory pins`, with the `telemetry-backfill: audit-readiness` trailer.

---

### Task 6: Web — default surface and upload-only guard

**Files:**
- Modify: `src/elspeth/web/plugin_policy/compiler.py` — add `PluginId("transform", "file_inject")` to the default set, sorted.
- Modify: `src/elspeth/web/execution/_validation_materialization.py`
- Modify: `src/elspeth/web/execution/service.py` (two call sites, ~1296 and ~3830)
- Modify: `src/elspeth/web/composer/tools/blobs.py` (`_llm_authored_prompt_surface_error`, `_llm_authored_inline_prompt_surface_error`, and the message builder)
- Test: `tests/unit/web/composer/test_inline_blob_prompt_surface_tool_parity.py`, `tests/unit/web/execution/test_validate_blob_inline.py`, `tests/unit/web/execution/test_service.py`, `tests/unit/web/catalog/test_policy_view.py`

**Interfaces:**
- Produces, in `_validation_materialization.py`:
  - `FILE_INJECT_CONTENT_OPTION_ROOT: Final = "files"`
  - `guarded_inline_surface_field(field_path: str, *, node_plugins: Mapping[str, str]) -> tuple[str, str, str] | None` — returns `(node_name, option_path, plugin)` when the path is an `llm` prompt surface on an `llm` node, or at or beneath `files` on a `file_inject` node.
  - `is_llm_authored_prompt_surface_binding(field_path, *, node_plugins: Mapping[str, str], creation_modality) -> bool` — the `llm_node_names` parameter is replaced; every caller passes a node-name → plugin map built from the representation it holds.
  - `llm_prompt_surface_field` keeps its signature and behaviour.
- Refusal text for a `file_inject` binding (one text for every tool, built in `_llm_authored_prompt_surface_message`):
  `"{tool_name} cannot wire LLM-authored blob '{blob_id}' into file_inject node '{node_id}' option '{option_path}': that content is written into row data and can reach an LLM prompt without the prompt review. Only user-uploaded files may back file_inject content. Ask the user to save the text as a file and upload it, then attach the uploaded blob with wire_blob_inline_ref."`
- The `llm` refusal text is unchanged byte for byte.

- [ ] **Step 1: Write the failing tests.** For each of the three enforcement points, a refusal and its control:

```python
# tool parity (parametrized over wire_blob_inline_ref, upsert_node, patch_node_options, splice_transform, set_pipeline,
# reusing the file's existing harness)
test_llm_authored_blob_refused_for_file_inject_content       # result text contains "save the text as a file and upload it"
test_user_uploaded_blob_accepted_for_file_inject_content     # control: same call, uploaded blob, succeeds
test_llm_authored_blob_still_allowed_on_reference_join       # unchanged behaviour, pinned
# /validate
test_validate_refuses_llm_authored_file_inject_ref
test_validate_accepts_user_uploaded_file_inject_ref
# run admission
test_run_admission_refuses_llm_authored_file_inject_ref      # InlineBlobPromptSurfaceAdmissionError
test_run_admission_accepts_user_uploaded_file_inject_ref
# predicate
test_guard_covers_nested_marker_beneath_files                # "node:n.options.files" and "...files.A" and "...files.A.content"
test_guard_ignores_files_option_on_other_plugins
# policy
test_file_inject_is_in_default_web_surface
```

- [ ] **Step 2: Run** the four test files with `-n 0` — expect the new tests FAIL.
- [ ] **Step 3: Implement** the predicate change and update the three call sites and the message builder.
- [ ] **Step 4: Mutation check.** Temporarily make `guarded_inline_surface_field` return `None` for `file_inject`; confirm the three refusal tests go red; restore.
- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/unit/web tests/integration/web > .claude/lanes/task6.log 2>&1; echo "exit=$?"` — expect `exit=0`.
- [ ] **Step 6: Commit** — `feat(web): authorize file_inject and refuse planner-written content for it`.

---

### Task 7: Planner guidance

**Files:**
- Modify: `src/elspeth/web/composer/skills/pipeline_composer.md` (next to the `reference_join` blob-wiring passage, ~line 664)
- Modify: `src/elspeth/web/composer/skills/pipeline_capabilities.md` (next to the `reference_join` passage, ~line 389)
- Test: `tests/unit/web/composer/test_prompts.py`

**Interfaces:** none.

The passage states: what `file_inject` is for; `mode: select` (with `key_field`, `output_field`, `on_miss`) versus `mode: all` (per-entry `output_field`); `format` per entry; the option path `node:<node_id>.options.files.<entry_name>.content`; and this instruction verbatim:

> Only files the user uploaded can be attached to `file_inject`. If the user wants text injected that they have not uploaded, tell them they need to save it as a file and upload it; attach it only once the upload exists. Do not create the blob yourself.

- [ ] **Step 1: Write the failing test** `test_file_inject_guidance_names_path_and_upload_rule` asserting the composed skill text contains `options.files.<entry_name>.content` and `save it as a file and upload it`.
- [ ] **Step 2: Run** it with `-n 0` — expect FAIL.
- [ ] **Step 3: Add the passages.** Check whether either skill file is covered by a byte or hash pin (search `tests/` for the file name) and re-pin through its generator if so.
- [ ] **Step 4: Run** `tests/unit/web/composer/test_prompts.py tests/unit/web/composer/test_planner_authoring_aids.py -n 0` — expect PASS.
- [ ] **Step 5: Commit** — `docs(composer): teach the planner file_inject and the upload rule`.

---

### Task 8: Example, integration tests and documentation

**Files:**
- Create: `examples/file_inject/settings.yaml` (`select` on `type`), `examples/file_inject/settings_all.yaml`, `examples/file_inject/input.csv`, `guidance_a.md`, `guidance_b.json`, `guidance_c.csv`, `README.md`
- Modify: `examples/README.md`, `examples/AGENTS.md`, `tests/e2e/examples/test_shipped_examples.py`
- Create: `tests/integration/pipeline/test_file_inject_example.py`
- Modify: `docs/reference/configuration.md`, `docs/guides/yaml-authoring.md`, `docs/guides/user-manual.md`

**Interfaces:** none.

The examples use a CSV source and a JSON sink and need no LLM or network, so they run in CI. Model the layout on `examples/reference_join/`. The README shows the downstream template line `{{ row.guidance }}` to explain the use case without making the example depend on a provider.

- [ ] **Step 1: Write the failing integration tests:**

```python
test_select_example_writes_matching_guidance_per_row     # row type A -> text of guidance_a.md; B -> parsed JSON; C -> list of dicts
test_all_example_writes_every_field_on_every_row
test_resume_is_refused_after_a_bound_file_is_edited      # follow the resume-refusal pattern in tests/integration/pipeline/test_reference_join_example.py
test_field_is_usable_in_an_llm_template                  # file_inject -> llm with the repo's existing offline LLM test double; the rendered prompt contains the A guidance for a type-A row
```

- [ ] **Step 2: Run** `tests/integration/pipeline/test_file_inject_example.py -n 0` — expect FAIL.
- [ ] **Step 3: Write the example files**, register the example in `test_shipped_examples.py`, and write the three doc sections (options table, both modes, formats, the upload-then-attach flow).
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/integration/pipeline/test_file_inject_example.py tests/e2e/examples -n 0`, then `.venv/bin/elspeth run --settings examples/file_inject/settings.yaml --execute` and the `settings_all.yaml` variant — expect PASS and exit 0, and read one output row of each.
- [ ] **Step 5: Run the doc link contracts** — `.venv/bin/python -m pytest tests/unit/architecture -n 0` — expect PASS.
- [ ] **Step 6: Commit** — `docs(file_inject): example, integration coverage and reference docs`.

---

### Task 9: Whole-branch gates

**Files:** none, unless a gate finds something.

- [ ] **Step 1: Trust-tier before/after.** Run the keyless scan on `origin/release/0.8.11-candidate` (in a second throwaway worktree) and on HEAD, each to a file under `.claude/lanes/`; diff the finding sets. Record the added findings in the hand-back; do not suppress or sign them.
- [ ] **Step 2:** `.venv/bin/ruff check . && .venv/bin/ruff format --check .` — expect clean.
- [ ] **Step 3:** `.venv/bin/mypy src` (or the invocation in `scripts/full-suite-gate.sh`) — expect clean.
- [ ] **Step 4: Full suite.** `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest`; poll the printed `.done` path; read `summary.txt`. Expect every stage exit 0 except `lints`, which is compared per Step 1, and `frozen=YES`. For a red in `e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator`, re-run the named test with `-n 0` before treating it as caused by this branch.
- [ ] **Step 5:** `scripts/branch-safety-check.sh --intent push`, then `git push -u origin claude/happy-cray-mbfmm1`.
- [ ] **Step 6: Raise the follow-up** for guarding `reference_join.reference_content` against planner-written blobs as a GitHub issue, only after the user confirms they want it filed.
