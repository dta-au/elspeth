# Research lane 2 — whole-tree pin inventory for ONE new transform `pdf_rasterize`

Read-only sweep at `a371e13d0`, 2026-08-25. Numbers are ONE-plugin (33→34 etc.).
Corrections to prior briefs: `EXPECTED_VARIANT_COUNT` lives in
`scripts/state_engine_plugin_matrix.py:44`, not test_discovery.py (which has bare
`== 51` at `:300` and `== 72` at `:323`); `boundary_expectations.py` live range is
`:127-193`, adding-a-plugin instructions `:69-77`.

## 0. Determinism decision forks four pins

| Pin | DETERMINISTIC | IO_READ | EXTERNAL_CALL |
|---|---|---|---|
| `EXPECTED_BOUNDARY_TRANSFORMS` (derived, `boundary_expectations.py:188-193`) | out | in | in |
| catalogue-metadata file | core (fail-open) | core (fail-open) | external — HARD FAIL until added |

Plan decision: **IO_READ** (reads the payload store; blob_csv_expand precedent).

## 1. Hard exact-set / count pins (same commit)

| # | path:line | Current | New |
|---|---|---|---|
| 1 | `tests/unit/plugins/test_discovery.py:265` | `EXPECTED_TRANSFORM_COUNT = 33` | `34` + rationale comment |
| 2 | `tests/unit/plugins/test_discovery.py:300` | `len(live_keys) == 51` | `52` |
| 3 | `tests/unit/plugins/test_discovery.py:323` | `len(expected_pairs) == 72` | `73` |
| 4 | `tests/unit/plugins/test_catalog_reference_content.py:213` | `len(REFERENCES) == 51` | `52` |
| 5 | `...:214-218` | `Counter == {"source": 9, "transform": 33, "sink": 9}` | `"transform": 34` |
| 6 | `...:34-86` `EXPECTED_BUILTIN_IDENTITIES` | 51 members | + `"transform:pdf_rasterize"` after `transform:passthrough` |
| 7 | `...:256` | `len(DIRECT_CONFIG_REFERENCES) == 47` | `48` (plugin is USER_CONFIGURABLE default) |
| 8 | `tests/unit/web/catalog/test_service.py:60` | `sum(...) == 51` | `52` |
| 9 | `tests/golden/web/catalog/knob_schema/` | 51 files | + `transform__pdf_rasterize.json` |
| 10 | `tests/unit/web/catalog/test_knob_schema_golden.py:39-40` | glob set-equality | derived |
| 11 | `src/elspeth/web/audit_readiness/boundary_expectations.py:150-183` `EXPECTED_TRANSFORM_DETERMINISMS` | 33 entries | + `"pdf_rasterize": Determinism.IO_READ` (PRODUCTION) |
| 12 | `tests/unit/web/audit_readiness/test_boundary_predicate_parity.py:140,144` | derived from #11 | — |

#9 regeneration: the test builds
`_stable_json({"plugin_kind","plugin_name","knob_schema"})` from
`CatalogServiceImpl(get_shared_plugin_manager())._schema_cache[(kind,name)].knob_schema`,
serialized `json.dumps(payload, indent=2, sort_keys=True) + "\n"`. Generate by that
exact expression AFTER the config model is final.

## 2. State-engine PB-09 — THREE catalogs (v3, v2, golden matrix)

| # | path:line | Current | New |
|---|---|---|---|
| 13 | `scripts/state_engine_plugin_matrix.py:43` | `EXPECTED_COUNTS = {..."transform": 33...}` | `34` |
| 14 | `scripts/state_engine_plugin_matrix.py:44` | `EXPECTED_VARIANT_COUNT = 72` | `73` |
| 15 | `scripts/state_engine_plugin_matrix.py:602` | `"...valid (51 plugins, ..."` | `52 plugins` (string IS a test pin, #25) |
| 16 | `scripts/state_engine_plugin_matrix.py:160,:293` | `"expected 51 plugins..."` ×2 | `52` |
| 17 | `tests/golden/state_engine/plugin_lifecycle_matrix.json` | 51 entries / 72 variants | regenerate + adjudicate 5 reviewed fields |
| 18 | `docs/architecture/state_engine/proof-catalog/v3/catalog.json` PB-09 `required_cases` (leg from `:12687`) | 72 | 73 — hand-add |
| 19 | `.../v3/catalog.json:140` `execution_profiles.first_party_plugins.transforms` | 33 names | + `pdf_rasterize` (forced by `test_state_engine_catalog_contract.py:222-236`, v3 == v2 execution_profiles) |
| 20 | `.../v3/evidence_selectors.json` lane `local-sqlite-wal-single-process-leader` | 712 node_ids | +5 node_ids (below) |
| 21 | `.../v2/catalog.json` `execution_profiles.first_party_plugins.transforms` | 33 | + `pdf_rasterize` (forced by `test_state_engine_catalog_contract.py:54-67`) |
| 22 | `.../v2/catalog.json` PB-09 `required_cases` (flat string list) | 51 | 52 (forced by `:154-167`) |
| 23 | `tests/unit/architecture/test_state_engine_catalog_contract.py:35` `V2_CATALOG_SHA256` | `802734e0...` | ROTATE after #21/#22 (`:174-175` pins it) |
| 24 | `...test_state_engine_catalog_contract.py:167` | `len(expected) == 51` | `52` |
| 25 | `tests/unit/plugins/test_state_engine_plugin_matrix.py:38` | `"51 plugins" in stdout` | `"52 plugins"` |
| 26 | `...:46` | `== [9, 33, 9]` | `[9, 34, 9]` |
| 27 | `...:73,:79` | fn name `..._the_72_reviewed_subjects`; `len(expected) == 72` | 73 both — rename the function |
| 28 | `tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py:391` | `len(LOCAL_LIFECYCLE_CASES) == 34` | `35` (external_observation_required: false) |
| 29 | `...:141` + `:211-212` | `"transform:blob_csv_expand": [{"blob_ref": "filled-by-harness"}]` and `if case.plugin_key == "transform:blob_csv_expand": rows[0]["blob_ref"] = store.store(...)` | add the analogous pair for pdf_rasterize storing a minimal one-page PDF |

v1 catalog is frozen (SHA pinned, never compared to live) — do not touch.

Reviewed-field vocabulary for #17 (`scripts/state_engine_plugin_matrix.py:60-66`,
validated `:511-529`): `variants` = `["default"]`; `external_observation_required`
bool; `applicable_pb_boundaries` ⊆ `{PB-01,PB-02,PB-04,PB-06,PB-07,PB-09}` (both exploder
precedents use `["PB-02","PB-09"]`); `local_fixture` ∈ `hermetic | provider-contract-fake
| real-process-http`; `release_lane` ∈ `local | live-aws | ...`. Precedent
`blob_csv_expand` at matrix `:1105-1132`: `determinism: io_read`, overrides
`on_start`/`process`/`close`, `hermetic` / `local` / `external_observation_required: false`.

The 5 node_ids for #20 (lane `local-sqlite-wal-single-process-leader`):
```
tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py::test_local_first_party_plugin_crosses_the_production_lifecycle[sqlite-wal-single-process-leader::transform:pdf_rasterize]
...::test_partial_start_failure_never_cleans_the_unstarted_subject[sqlite-wal-single-process-leader::partial-start::transform:pdf_rasterize]
...::test_later_start_failure_cleans_the_already_started_subject_in_order[sqlite-wal-single-process-leader::partial-cleanup::transform:pdf_rasterize]
...::test_exceptional_operation_still_completes_and_closes_the_started_subject[sqlite-wal-single-process-leader::exceptional::transform:pdf_rasterize]
...::test_public_resume_reconciles_the_subject_pipeline_after_a_finalized_effect_response_loss[sqlite-wal-single-process-leader::resume::transform:pdf_rasterize]
```

## 3. Hand-maintained allowlists the hints doc omits

| # | path:line | Rule |
|---|---|---|
| 30 | `tests/invariants/test_input_schema_config_is_captured.py:80-87` `_EXPECTED_MUTATION_REJECTIONS` | add `pdf_rasterize` iff its config rejects the synthetic mutation (`:185` subset assert → unlisted rejection HARD-FAILS) |
| 31 | `tests/invariants/test_transform_input_contract_is_satisfiable.py:85-92` `_EXPECTED_ARMING_REJECTIONS` | same rule (`:313`) |
| 32 | `config/cicd/contracts-whitelist.yaml:173-177` (`probe_config:return`) | + `"src/elspeth/plugins/transforms/pdf_rasterize.py:PDFRasterize.probe_config:return"` |
| 33 | `config/cicd/contracts-whitelist.yaml:216-222` (constructor) | + `"src/elspeth/plugins/transforms/pdf_rasterize.py:PDFRasterize.__init__:options"` — segment must match the ACTUAL param name |
| 34 | `tests/unit/plugins/test_validation_path_agreement.py:31` `_TRANSFORM_REJECTION_CASES` | add a rejection case iff the config has a `@model_validator` (`:653-724` AST-scans) |

Consumers for #32/#33: `scripts/check_contracts.py:1269`, pre-commit `check-contracts`
(`.pre-commit-config.yaml:214-224`, fires LOCALLY), `ci.yaml:166`;
`scripts/codex_exemption_validator.py:312-313,396` fails on entries pointing at a
nonexistent symbol. Whitelist additions need a paired cleanup ticket per house rule.

## 4. Catalogue-metadata partition

`test_external_catalogue_metadata.py:53-57` membership derived from determinism ∈
{EXTERNAL_CALL, NON_DETERMINISTIC}; IO_READ does not fire it.
`test_core_catalogue_metadata.py:20-38` is by-name and self-satisfying; house style says
add `pdf_rasterize` to `EXPECTED_CORE_TAGS` and `_REQUIRED_GUIDANCE` (`:43-54`) anyway.
Shape gate fires regardless (`test_catalog_reference_content.py:222-226` →
`tests/fixtures/catalog_reference.py:88-98`). Global uniqueness of the three prose
strings: `test_catalog_reference_content.py:293-316`.

## 5. Prose pins in other plugins

| # | path:line | Constraint |
|---|---|---|
| 35 | `tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py:812-823` | hints must still contain `blob_rows`, `jpeg`, `single-page`, `aws_textract_document_analysis`, `billable` |
| 36 | `textract_inline_analysis.py:556` | `Pages != 1` guard stays; test already exists at `:646-656` |

## 6. Production code the plugin must satisfy (no edit)

- `discovery.py:286-291` `PLUGIN_SCAN_CONFIG` non-recursive: `transforms/pdf_rasterize.py` IS scanned; `plugins/infrastructure/rasterize/` is NOT.
- `discovery.py:18-25` `OPTIONAL_PLUGIN_IMPORT_MODULES` — do NOT add `pypdfium2` (pinned at `test_discovery.py:668`).
- `base.py:1612` `policy_capabilities` — leave default (`compiler.py:135/:144` `incomplete_preference_order` startup failure otherwise).
- `test_state_engine_plugin_lifecycle_matrix.py:82-101` — `example_use` must have exactly one node with `plugin: pdf_rasterize` and a Mapping `options`.
- `web/execution/fanout_guard.py:406-412` derives from `creates_tokens` — no enrolment needed.
- `pyproject.toml:305-312` — no-`.get()` policy; use `[...]` indexing.
- `config/cicd/masquerade_baseline.yaml` — design to need no entry (no getattr/hasattr in plugin or tests).

## 7. Silent / fail-open sites — decide explicitly

- `web/interpretation_state.py:165-167` `_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS` — FAIL-OPEN; pdf_rasterize does NOT join (reasoning in lane 4 §7).
- `web/composer/guided/_display.py:29-46` `_ACRONYMS` — no `pdf` → humanises to "Pdf Rasterize". Add `"pdf"`.
- `web/frontend/src/components/catalog/pluginDisplayName.ts:23-38` `ACRONYMS` — hand-mirrored, NO parity test; add `"pdf"` too.
- `web/composer/state.py:7715-7723` `_plugins_requiring_config` — optional advisory enrolment.
- `source_file_hash` for `plugins/transforms/*.py` is CI-only (`ci.yaml:181,:301`); no local test recomputes it.

## 8. Packaging / CI / container

| # | Item |
|---|---|
| 37 | `pyproject.toml:75-91` base deps — add `"pypdfium2>=5.13,<6",  # backs pdf_rasterize transform` under `# === Plugin catalog dependencies ===` |
| 38 | `all` extra (`:196-253`) — base deps never appear there; nothing to do |
| 39 | `uv.lock` — regenerate with `uv lock` (lock only; no sync in the worktree); CI uses `--frozen` everywhere |
| 40 | `pyproject.toml:369-377` mypy stubless block — add `"pypdfium2", "pypdfium2.*", "pypdfium2_raw", "pypdfium2_raw.*"` (wheel has no `py.typed`, VERIFIED after install) |
| 41 | `ci.yaml:762-763` pip-licenses `--fail-on "GPL;AGPL"` — METADATA `License: BSD-3-Clause, Apache-2.0, dependency licenses` passes |
| 42 | `ci.yaml:750-757` pip-audit — no entry unless an advisory exists; run locally |
| 43 | `Dockerfile:144` distroless — VERIFIED by lane 3: bundled `libpdfium.so` needs only glibc ≥2.16, libgcc_s, libm, libpthread; runs in the exact runtime image digest |
| 44 | `.github/workflows/build-push.yaml:139-152` import smoke — RECOMMENDED: add `import pypdfium2` to the heredoc |
| 45 | `scripts/cicd/plugin_hash.py:85` — library, no CLI; canonical heredoc at `docs/contracts/plugin-catalogue-reference-content.md:141-152` |
| 46 | `elspeth-lints .../plugin_hashes/rule.py:23-31` — new plugin IS hash-checked; `EXPECTED_PLUGIN_COUNT = 45` is a floor |
| 47 | wardline — no per-directory rule; agent-loop gate only |
| 48 | `config/cicd/enforce_tier_model/plugins.yaml` — no blanket for `plugins/transforms/*`; any new suppression needs a signed judge verdict — design to avoid |
| 49 | `enforce_component_type` / `enforce_options_metadata` — do not fire provided every config field has `title` + `description` |

Unverified gap: `config/cicd/symbol_inventory/migration_files.yaml` and
`config/cicd/test_to_source_mapping/migration_files.yaml` (whole-repo gates at
`ci.yaml:226-231`) may map new test files to new source files — read before fixing the
test-file layout for `plugins/infrastructure/rasterize/`.

## 9. Planner digest byte budget

`web/composer/planner_authoring_aids.py:1044` cap 24,576; overflow silently strips
prose from ALL plugins (`:1637-1645`). Measure: `discovery_digest(catalog)["budget"]["canonical_bytes_used"]`.
Spec-recorded: 20,386 → 21,410 with two entries. Keep new prose at house length;
re-measure AFTER reference text is final.

## 10. Docs (unpinned but incomplete without)

`docs/reference/configuration.md:917` (transform table, after `blob_csv_expand`);
`docs/guides/user-manual.md:188` (`plugins list` transcript, between `passthrough` and
`report_assemble`); `docs/agents/recent-code-hints.md:433` ("All 41 builtins" → 52) and
§6 `:1061-1093` (add the omitted sites); `docs/guides/docker.md:364` counts;
`examples/README.md` only if a new example dir is added (`test_examples_readme_index.py`).
`docs/architecture/state_engine/assessments/**` is frozen — never edit.

## 12. Ordering constraints

1. Config model final → regenerate knob-schema golden (#9).
2. Pre-edit `state_engine_plugin_matrix.py:43-44` (#13/#14) — `render-skeleton` dies otherwise.
3. `uv run python scripts/state_engine_plugin_matrix.py render-skeleton tests/golden/state_engine/plugin_lifecycle_matrix.json` — writes in place, exits 1 by design → hand-adjudicate → rerun until exit 0.
4. Hand-add v3 PB-09 (#18), v3+v2 `first_party_plugins` (#19/#21), v2 PB-09 (#22) → rotate `V2_CATALOG_SHA256` (#23) LAST.
5. `validate-catalog` → `state_engine_plugin_matrix.py check` → `validate-selectors` (strictly last).
6. Reference text final → re-measure planner digest.
7. `ruff format` → THEN `source_file_hash` (#45).
8. `uv lock` after the pyproject edit, before anything runs.
9. Full `pytest tests/` before merge.
