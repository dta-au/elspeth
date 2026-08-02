# Source-Native Single-Prompt LLM Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` or `superpowers:executing-plans`, and test-drive each unfinished task. Work only in `/home/john/elspeth/.claude/worktrees/llm-source` on `codex/llm-source`.

**Goal:** Add a pluggable source-only `llm` plugin that takes one authored prompt, makes one provider call without input rows, and emits exactly one row whose configurable response, usage, and model fields match the LLM transform contract, while closing every source-kind required-control, audit, runtime, discovery, and authoring parity gap.

**Architecture:** `LLMSource` implements `BaseSource` directly. It never constructs a transform or synthetic input row. Provider calls share neutral LLM validation, catalogue, finish-reason, operational-field, tracing, and provider-adapter primitives. The real `source_load` operation is the audit parent; no state or token exists before a row is yielded. Content Safety must post-dominate generated output, including alternate source exits; Prompt Shield remains deliberately transform-input-only because this source prompt is author-authored static/lookup configuration.

**Output contract:** If `response_field: answer`, the source emits `answer`, `answer_usage`, and `answer_model`. `populate_llm_operational_fields()` is authoritative: usage is reported-provider data only. Preserve known, partial, unknown, and internally inconsistent totals exactly; never synthesize `total_tokens` from prompt and completion counts.

**Ordering invariant:** Treat production built-in discovery as public exposure because it feeds catalogue and authoring surfaces. Do not enable it, web-authorize the source, or make the source Composer-authorable until required-control coverage, execution-time fail-closed validation, auto-wire behavior, backend execution policy, audit-readiness, runtime profile/value-source/availability behavior, and authored-prompt consent enumeration are complete. Task 9 uses isolated test registration only; Task 10 publishes compiler/catalogue support, frontend consent, and built-in discovery atomically. There must be no commit where the source is Composer-expressible but absent from outbound-LLM consent.

**Constraints:** Preserve transform behavior, source routing, profile provenance, and cleanup primacy. Do not add a provider preflight request. Do not hand-invent plugin hashes or catalogue goldens. Do not refactor bounded provider-factory/schema duplication in this feature. Do not edit or sign trust-tier judge metadata. Do not push, merge, open a PR, or alter the shared checkout without a new request.

---

## File map

### Implemented in Tasks 1-3

- `src/elspeth/plugins/llm/{__init__.py,config_validation.py,model_catalog.py}` — component-neutral validation and model catalogue.
- `src/elspeth/plugins/sources/llm/{__init__.py,config.py,source.py}` — source config, lifecycle, provider construction, schema handling, assistance, and reference prose.
- `src/elspeth/plugins/transforms/llm/{provider.py,templates.py,transform.py,langfuse.py,model_catalog.py,__init__.py}` and `providers/{azure.py,bedrock.py,gateway.py,openrouter.py}` — shared audit parent, rendering, tracing, usage, provider, and compatibility seams.
- `src/elspeth/contracts/{contexts.py,events.py,__init__.py}`, `src/elspeth/plugins/infrastructure/{clients/base.py,telemetry.py}`, `src/elspeth/telemetry/{__init__.py,filtering.py}`, and `src/elspeth/engine/orchestrator/source_iteration.py` — lifecycle context, cleanup telemetry, bounded telemetry filtering, and source shutdown classification.
- `config/cicd/contracts-whitelist.yaml` and focused source/provider/contract/telemetry tests changed by the three commits.

### Remaining production surfaces, in dependency order

- Task 4: `src/elspeth/plugins/sources/llm/source.py`, source iteration, cleanup telemetry/tests.
- Tasks 5-6: `src/elspeth/web/plugin_policy/{coverage.py,validation.py}` and `src/elspeth/web/composer/required_controls.py`.
- Task 7: `src/elspeth/web/execution/validation.py` and `src/elspeth/web/audit_readiness/{boundary_expectations.py,explain.py,service.py}`.
- Task 8: `src/elspeth/core/config.py`, `src/elspeth/plugins/infrastructure/runtime_factory.py`, `src/elspeth/engine/orchestrator/preflight.py`, `src/elspeth/web/execution/preflight.py`, and `src/elspeth/web/plugin_policy/{availability.py,profiles.py}`.
- Task 9: isolated-registration real-engine integration tests only.
- Task 10: `src/elspeth/web/plugin_policy/{compiler.py,profiles.py}`, `src/elspeth/web/composer/planner_authoring_aids.py`, catalogue consumers, frontend consent, production discovery, plugin-hash inventory/declarations, and generated source goldens.
- Task 11: `examples/llm_source/`, example indexes, and shipped-example tests.

---

## Task 1: Add an operation-capable LLM audit parent — complete

**Commit:** `cc3d24899` (`refactor: support operation-parented LLM provider calls`)

- [x] Add an owned, exactly-one-of row/operation `LLMAuditParent` with exact built-in string validation.
- [x] Convert Azure, Bedrock, OpenRouter, Gateway, and transform callers to the explicit parent.
- [x] Keep transform calls state/token-parented and make logical plus transport records share their parent.
- [x] Extract pure finish-reason classification without changing transform result semantics.
- [x] Preserve provider cleanup primacy and refcount/cache-key behavior.
- [x] Pass focused provider, transform, tracing, telemetry, and Gateway integration regressions and commit hooks.

## Task 2: Add source-specific config and static prompt rendering — complete

**Commit:** `79377065c` (`feat: define single-prompt LLM source configuration`)

- [x] Add source-rooted Azure, OpenRouter, Bedrock, and Gateway discriminated configs without inheriting transform config.
- [x] Reject transform-only options and row-bound templates; allow static prompt context, including `lookup`.
- [x] Add static rendering while keeping transform rendering byte-compatible.
- [x] Add neutral provider validators and model catalogue modules, retaining the transform import compatibility shim.
- [x] Add the source output-schema helper for configurable `<field>`, `<field>_usage`, and `<field>_model` fields.
- [x] Verify isolated registration/import behavior and 1,170 focused configuration, template, schema, catalogue, and provider-validation tests.

## Task 3: Implement the source-native one-call, one-row plugin — complete, subject to Task 4

**Commit:** `1994bf969` (`feat: add source-native single-prompt LLM plugin`)

- [x] Implement `LLMSource(BaseSource)` directly with one admitted load, one provider call, and at most one row at `source_row_index == 0`.
- [x] Implement four provider constructors, operation-aware Langfuse tracing, finish-reason failure, fence stripping, schema validation, and managed iterator closure.
- [x] Use `populate_llm_operational_fields()` exactly. Known, partial, unknown, and inconsistent provider totals remain reported values; no omitted total is computed.
- [x] Add cleanup-failure telemetry, provider cleanup primacy, pre-egress shutdown handling, and narrow engine `INTERRUPTED` classification.
- [x] Add source assistance/reference/output semantics and contract tests without public built-in discovery.
- [x] Verify the 1,350-test regression slice, full mypy over 710 source files, Ruff and formatting, `elspeth-lints check`, and the exact non-inert Wardline gate (710 files, 59 boundaries, zero active ERROR findings).

Task 4 reopens two lifecycle invariants found by review; do not advance around it.

## Task 4: Repair real lifecycle shutdown primacy and telemetry correlation

**Files:**

- Modify: `src/elspeth/plugins/sources/llm/source.py`
- Modify if required: `src/elspeth/engine/orchestrator/source_iteration.py`
- Test: `tests/unit/plugins/sources/llm/test_source.py`
- Test: `tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py`
- Test: `tests/unit/telemetry/exporters/test_console.py`

- [ ] Write a real-order lifecycle regression: `on_start()` precedes the engine's `source_load` operation and sees no operation ID; `load()` receives and stores the real operation ID for teardown correlation.
- [ ] Test shutdown set before first iteration with provider close, tracer flush, and dual cleanup failures. The primary result remains shutdown/`INTERRUPTED`; cleanup never replaces it, and provider/tracing egress remains zero.
- [ ] Assert post-load `ResourceCleanupFailed` events carry real run ID, `source_load` operation ID, resource, and suppression status, with no invented state/token IDs.
- [ ] Keep initialization failure before `load()` bounded and honest: without a real operation, emit no fabricated correlation.
- [ ] At load admission, store the validated operation ID. Ensure the managed session establishes primary-outcome suppression before testing shutdown, detaches resources before closure, and attempts both provider and tracer cleanup.
- [ ] Run and commit:

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/sources/llm/test_source.py \
  tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py \
  tests/unit/telemetry/exporters/test_console.py -q
git add src/elspeth/plugins/sources/llm/source.py src/elspeth/engine/orchestrator/source_iteration.py tests/unit/plugins/sources/llm/test_source.py tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py tests/unit/telemetry/exporters/test_console.py
git commit -m "fix: preserve LLM source shutdown primacy"
```

## Task 5: Make required-control coverage component-typed and fail closed at execution

Public discovery remains off. Tests may register `LLMSource` explicitly in an isolated manager before exercising policy.

**Files:**

- Modify: `src/elspeth/web/plugin_policy/{coverage.py,validation.py}`
- Modify: `src/elspeth/web/execution/validation.py`
- Test: `tests/unit/web/plugin_policy/{test_coverage.py,test_validation.py}`
- Test: relevant required-control cases in `tests/unit/web/execution/test_validation.py`
- Test: `tests/integration/web/test_execute_pipeline.py`

- [ ] Add red tests proving every finding carries stable `component_id` and `component_type` (`source` or `transform`) through policy and execution validation.
- [ ] For Content Safety, treat only the configured LLM response field as generated output and require downstream post-domination on every success path.
- [ ] The current graph cannot safely control a source `on_validation_failure` alternate route. Under `ControlMode.REQUIRED`, every non-`discard` LLM source validation-failure destination is therefore unrepairable and rejected without exception, even if the success route is covered. `discard` is accepted.
- [ ] Cover direct-to-sink rejection, branched and mixed graphs, singular policy-state IDs, named/multiple sources, custom response fields, stable order, covered success with discard, and execution-time fail-closed rejection.
- [ ] Keep Prompt Shield transform-only and pin that explicit ruling.
- [ ] Run and commit:

```bash
.venv/bin/python -m pytest \
  tests/unit/web/plugin_policy/test_coverage.py \
  tests/unit/web/plugin_policy/test_validation.py \
  tests/unit/web/execution/test_validation.py \
  tests/integration/web/test_execute_pipeline.py -q
git add src/elspeth/web/plugin_policy/coverage.py src/elspeth/web/plugin_policy/validation.py src/elspeth/web/execution/validation.py tests/unit/web/plugin_policy/test_coverage.py tests/unit/web/plugin_policy/test_validation.py tests/unit/web/execution/test_validation.py tests/integration/web/test_execute_pipeline.py
git commit -m "fix: require content safety for LLM sources"
```

## Task 6: Auto-wire only repairable source success routes

**Files:**

- Modify: `src/elspeth/web/composer/required_controls.py`
- Test: `tests/unit/web/composer/test_required_control_autowire.py`
- Test: `tests/integration/web/composer/guided/test_shared_planner_surfaces.py`

- [ ] Add red splice tests for the Composer's legacy singular `source` candidate and plural `sources` candidate. Runtime YAML remains the plural `sources` mapping only; cover its conventional key `source` and arbitrary source names.
- [ ] Preserve the authored singular/plural container provenance. If both legacy singular and plural forms appear, treat the candidate as ambiguous and return it unchanged by identity.
- [ ] Splice only a repairable LLM source with `on_validation_failure: discard`: source -> new intermediate stream -> Content Safety -> original success stream. Preserve the old success destination exactly.
- [ ] Leave every non-discard validation-failure candidate unchanged by identity for Task 5's fail-closed validator to reject; never partially certify it.
- [ ] Cover already-covered/no-op identity, second-pass idempotence, multiple sources, mixed source/transform graphs, reserved stream/node collisions, malformed candidates, no Prompt Shield, and bounded splice budget.
- [ ] Reparse after each splice and preserve stable disclosure/provenance data.
- [ ] Run and commit:

```bash
.venv/bin/python -m pytest \
  tests/unit/web/composer/test_required_control_autowire.py \
  tests/integration/web/composer/guided/test_shared_planner_surfaces.py -q
git add src/elspeth/web/composer/required_controls.py tests/unit/web/composer/test_required_control_autowire.py tests/integration/web/composer/guided/test_shared_planner_surfaces.py
git commit -m "fix: auto-wire content safety after LLM sources"
```

Do not close the P1 here; closure is Task 12 only.

## Task 7: Enforce backend execution policy and explain audit readiness

**Files:**

- Modify: `src/elspeth/web/execution/validation.py`
- Modify: `src/elspeth/web/audit_readiness/{boundary_expectations.py,explain.py,service.py}`
- Test: `tests/unit/web/execution/`
- Test: `tests/integration/web/test_execute_pipeline.py`
- Test: `tests/unit/web/audit_readiness/`

- [ ] Apply LLM base-URL, tracing, selected-profile attribution, and required-control gates to source components with source IDs/types. Keep sequential multi-query retry policy transform-only.
- [ ] Keep static-row-prompt and interpretation review checks transform-only; a rowless source does not create row interpretation events.
- [ ] Add `llm` as a non-deterministic external source boundary. Describe one authored prompt, one generated row, served model, reported usage, timestamp, and operation record.
- [ ] Make `llm_interpretations` explicitly not applicable when only the source exists, with source-specific narrative rather than transform prose.
- [ ] Run and commit as a backend-only slice:

```bash
.venv/bin/python -m pytest \
  tests/unit/web/execution \
  tests/integration/web/test_execute_pipeline.py \
  tests/unit/web/audit_readiness -q
git add src/elspeth/web/execution/validation.py src/elspeth/web/audit_readiness tests/unit/web/execution tests/integration/web/test_execute_pipeline.py tests/unit/web/audit_readiness
git commit -m "fix: enforce and explain LLM source boundaries"
```

## Task 8: Admit runtime profiles, value sources, and provider-aware availability

Register the source's internal operator-profile resolver in this task because provider-aware availability depends on it. Do not yet add the source to `REQUIRED_WEB_PLUGIN_IDS`, publish its public schema projection, expose it through the catalogue, or enable built-in discovery.

**Files:**

- Modify: `src/elspeth/core/config.py`
- Modify: `src/elspeth/plugins/infrastructure/runtime_factory.py`
- Modify: `src/elspeth/engine/orchestrator/preflight.py`
- Modify: `src/elspeth/web/execution/preflight.py`
- Modify: `src/elspeth/web/plugin_policy/availability.py`
- Modify: `src/elspeth/web/plugin_policy/profiles.py`
- Modify: `src/elspeth/plugins/sources/llm/source.py`
- Test: `tests/unit/core/test_llm_profile_catalog.py`
- Test: `tests/unit/web/plugin_policy/test_availability.py`
- Test: `tests/unit/web/plugin_policy/test_profiles.py`
- Test: `tests/unit/web/execution/{test_preflight_side_effects.py,test_validation_value_source.py}`
- Test: source config/runtime tests under `tests/unit/plugins/sources/llm/`

- [ ] Extend CLI/runtime lowering only over the plural `sources` mapping. Cover the conventional mapping key `source` and arbitrary names; there is no top-level singular runtime source contract.
- [ ] Lower source profiles even when `transforms` is absent/non-list. Preserve source `on_success` and `on_validation_failure`, reject unknown alias and ambiguous profile/provider, reject user-scoped CLI credentials, and accept server-scoped plus scope-less Bedrock profiles.
- [ ] Preserve the authored alias as `source.options.profile` in audit-safe settings. Retain executable `profile_alias` in `BaseSource.config` for runtime attribution, stripping only `profile_alias` immediately before strict source provider-config validation. A second lowering pass is identity-equivalent.
- [ ] Extend runtime factory and preflight value-source walks to source components with `source`/`source:<name>` attribution and `component_type == "source"`.
- [ ] Reject invalid OpenRouter catalogue model and Azure model/deployment pairing. Bedrock and Gateway declare no value sources and pass without fabricated checks.
- [ ] Remove the flat source `api_key` discovery requirement. Provider/profile-aware availability accepts keyless Bedrock with empty inventory and requires exactly the Azure, OpenRouter, or Gateway credential for those profiles; cover server/user inventory and unknown aliases without exposing values.
- [ ] Register the internal source `llm` profile resolver so `OPERATOR_PROFILED` availability can enumerate only usable source aliases. Cover configured aliases, no usable alias, wrong component kind, and fail-closed missing-profile behavior. Keep public source-schema projection and compiler admission deferred to Task 10.
- [ ] Keep source constructors network-free in preflight.
- [ ] Explicitly defer source/transform provider-factory and discriminated-schema duplication as bounded debt. Paired four-provider config, construction, and schema-parity tests protect it; do not refactor it now.
- [ ] Run and commit:

```bash
.venv/bin/python -m pytest \
  tests/unit/core/test_llm_profile_catalog.py \
  tests/unit/plugins/sources/llm \
  tests/unit/web/plugin_policy/test_availability.py \
  tests/unit/web/plugin_policy/test_profiles.py \
  tests/unit/web/execution/test_preflight_side_effects.py \
  tests/unit/web/execution/test_validation_value_source.py -q
git add src/elspeth/core/config.py src/elspeth/plugins/infrastructure/runtime_factory.py src/elspeth/engine/orchestrator/preflight.py src/elspeth/web/execution/preflight.py src/elspeth/web/plugin_policy/availability.py src/elspeth/web/plugin_policy/profiles.py src/elspeth/plugins/sources/llm/source.py tests/unit/core/test_llm_profile_catalog.py tests/unit/plugins/sources/llm tests/unit/web/plugin_policy/test_availability.py tests/unit/web/plugin_policy/test_profiles.py tests/unit/web/execution/test_preflight_side_effects.py tests/unit/web/execution/test_validation_value_source.py
git commit -m "feat: admit LLM source runtime profiles"
```

## Task 9: Prove the real engine through isolated registration

Production built-in discovery remains off. Register `LLMSource` explicitly in a fresh isolated plugin manager for these tests so the engine contract is proved without creating a catalogue-visible public path.

**Files:**

- Create: `tests/integration/plugins/llm/test_source_pipeline.py`
- Modify if required: `tests/integration/plugins/llm/conftest.py`
- Modify only if a red integration test exposes a defect: runtime source/provider/orchestrator files already owned by Tasks 1-8.

- [ ] Through the real orchestrator and Landscape, prove this exact provider row matrix under one `source_load` operation ID:
  - Azure: one logical LLM external-call row.
  - Bedrock: one logical LLM external-call row.
  - OpenRouter: one HTTP transport row plus one logical LLM row.
  - Gateway: one HTTP transport row plus one logical LLM row.
- [ ] On provider failures, the applicable rows are `ERROR`, share the same real operation ID, and have no state/token IDs. On success, exactly one source row at index 0 is emitted and state/token allocation occurs only after yield.
- [ ] Cover known/partial/unknown/inconsistent usage parity, output lineage to a persisted sink, exhausted source not reopened on resume, and interrupted-before-exhaustion resume refusal.
- [ ] Assert the isolated registration is scoped to the test manager and does not mutate the process-global shared manager or production discovery map.
- [ ] Run and commit the integration proof only:

```bash
.venv/bin/python -m pytest tests/integration/plugins/llm/test_source_pipeline.py -q
git add tests/integration/plugins/llm/test_source_pipeline.py tests/integration/plugins/llm/conftest.py
git commit -m "test: prove LLM source operation lineage"
```

## Task 10: Publish compiler, catalogue, consent, and discovery atomically

**Files:**

- Modify: `src/elspeth/web/plugin_policy/{compiler.py,profiles.py}`
- Modify: `src/elspeth/web/composer/planner_authoring_aids.py`
- Modify: catalogue consumers under `src/elspeth/web/catalog/`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx`
- Modify: `src/elspeth/plugins/infrastructure/discovery.py`
- Modify: `elspeth-lints/src/elspeth_lints/rules/plugin_contract/plugin_hashes/rule.py`
- Modify at the end only: `src/elspeth/plugins/sources/llm/source.py`
- Modify mechanically at the end only: `src/elspeth/plugins/transforms/llm/transform.py`
- Test: `tests/unit/web/plugin_policy/{test_compiler.py,test_profiles.py}`
- Test: `tests/unit/web/composer/test_planner_authoring_aids.py`
- Test: `tests/unit/plugins/sources/test_source_catalogue_metadata.py`
- Test: `tests/unit/web/catalog/{test_service.py,test_knob_schema_golden.py,test_policy_view_golden.py}`
- Test: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx`
- Test: `tests/unit/plugins/{test_discovery.py,test_builtin_plugin_metadata.py}`
- Test: `tests/unit/plugins/llm/test_plugin_registration.py`
- Test: `tests/unit/elspeth_lints/test_plugin_contract_rules.py`
- Create mechanically: `tests/golden/web/catalog/knob_schema/source__llm.json`
- Create mechanically: `tests/golden/web/catalog/policy_view/source__llm.json`

- [ ] Add public `PluginId("source", "llm")` compiler requirement only now that Tasks 5-9 are green. Publish the source resolver registered in Task 8 through a component-aware public schema; do not create a second resolver or change its availability semantics.
- [ ] Resolve provider variants from schema discriminator mappings, not transform class names. Preserve source `on_validation_failure`; exclude transform-only and private endpoint/credential/resolved-model fields. Re-run Task 8 profile/availability tests before enabling discovery.
- [ ] Add source-only planner aids, reference prose, value-source hints, and stale-catalog advisory text. Do not suggest an LLM transform for a requested generation-first topology.
- [ ] While production discovery is still off, test compiler, profile, catalogue, and authoring behavior with explicit isolated source registration.
- [ ] In the same pre-discovery phase, update `ExecuteButton` consent using a synthetic source catalogue. Cover current, failed, stale, not-yet-loaded, and unknown catalogue states. An LLM source sends one authored prompt—not source rows—and appears exactly once; ordinary-source and LLM-transform wording remains unchanged.
- [ ] Resolve the display alias only from authored `source.options.profile`, never a deployment default. Otherwise use a safe authored model, then generic `configured LLM`. Never reveal a private resolved model, endpoint, credential, or secret reference.
- [ ] Run the focused frontend test, typecheck, and lint before enabling discovery:

```bash
(cd src/elspeth/web/frontend && npm test -- src/components/sidebar/ExecuteButton.test.tsx && npm run typecheck && npm run lint)
```

- [ ] Only after those tests are green, add `sources/llm` to `PLUGIN_SCAN_CONFIG`; add `plugins/sources/llm` to the hash rule's `PLUGIN_DIRS`; update its expected built-in count from 37 to 38; and add nested-directory, fresh-process, canonical identity, repeated-discovery, built-in metadata, and shared-manager regressions.
- [ ] Add the helper-recognized placeholder declaration `source_file_hash: str | None = "sha256:0000000000000000"` to `LLMSource` with `apply_patch`, then mechanically compute/apply both the new source hash and the already-stale `LLMTransform` hash. Never guess a final hash or touch judge signatures.
- [ ] Generate goldens from live sorted `CatalogServiceImpl` output only after production discovery is enabled; never hand-author JSON.
- [ ] Run the whole exposure slice together:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash

targets = (
    (Path("src/elspeth/plugins/sources/llm/source.py"), "LLMSource"),
    (Path("src/elspeth/plugins/transforms/llm/transform.py"), "LLMTransform"),
)
for path, class_name in targets:
    fix_source_file_hash(path, class_name, compute_source_file_hash(path))
PY
.venv/bin/python -m pytest \
  tests/unit/core/test_llm_profile_catalog.py \
  tests/unit/web/plugin_policy/test_compiler.py \
  tests/unit/web/plugin_policy/test_profiles.py \
  tests/unit/web/plugin_policy/test_availability.py \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/test_discovery.py \
  tests/unit/plugins/test_builtin_plugin_metadata.py \
  tests/unit/plugins/llm/test_plugin_registration.py \
  tests/unit/web/catalog/test_service.py \
  tests/unit/web/catalog/test_knob_schema_golden.py \
  tests/unit/web/catalog/test_policy_view_golden.py \
  tests/unit/elspeth_lints/test_plugin_contract_rules.py -q
PYTHONPATH=src:elspeth-lints/src .venv/bin/elspeth-lints check --rules plugin_contract.plugin_hashes
git add src/elspeth/web/plugin_policy/compiler.py src/elspeth/web/plugin_policy/profiles.py src/elspeth/web/composer/planner_authoring_aids.py src/elspeth/web/catalog src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx src/elspeth/plugins/infrastructure/discovery.py elspeth-lints/src/elspeth_lints/rules/plugin_contract/plugin_hashes/rule.py src/elspeth/plugins/sources/llm/source.py src/elspeth/plugins/transforms/llm/transform.py tests/unit/core/test_llm_profile_catalog.py tests/unit/web/plugin_policy/test_compiler.py tests/unit/web/plugin_policy/test_profiles.py tests/unit/web/plugin_policy/test_availability.py tests/unit/web/composer/test_planner_authoring_aids.py tests/unit/plugins/sources/test_source_catalogue_metadata.py tests/unit/plugins/test_discovery.py tests/unit/plugins/test_builtin_plugin_metadata.py tests/unit/plugins/llm/test_plugin_registration.py tests/unit/web/catalog tests/unit/elspeth_lints/test_plugin_contract_rules.py tests/golden/web/catalog
git commit -m "feat: publish LLM source authoring support"
```

This commit is the first production built-in discovery/catalogue exposure and must be atomic with every compiler, profile, authoring, consent, metadata, golden, lint-inventory, and hash change above.

## Task 11: Add a bounded shipped example

**Files:**

- Create: `examples/llm_source/{settings.yaml,README.md}`
- Modify: `examples/{AGENTS.md,README.md}`
- Modify: `tests/e2e/examples/test_shipped_examples.py`
- Modify: `tests/unit/docs/test_examples_readme_index.py`

- [ ] Add a red inventory test requiring one `llm` source, no LLM transform, custom response field, explicit discard, and a JSON sink.
- [ ] Use the OpenRouter-compatible ChaosLLM loopback, one static prompt, observed schema, and one-row response/usage/model documentation. State that usage mirrors reported provider data.
- [ ] If local required Content Safety is unavailable, say the CLI example assumes recommend mode; do not add a fake control.
- [ ] Run and commit:

```bash
.venv/bin/python -m pytest tests/e2e/examples/test_shipped_examples.py tests/unit/docs/test_examples_readme_index.py -q
PYTHONPATH=src:elspeth-lints/src .venv/bin/elspeth validate --settings examples/llm_source/settings.yaml
git add examples/llm_source examples/AGENTS.md examples/README.md tests/e2e/examples/test_shipped_examples.py tests/unit/docs/test_examples_readme_index.py
git commit -m "docs: add single-row LLM source example"
```

Validation must make no provider request.

## Task 12: Classify every discriminator and run full closeout

Run these bounded production searches separately; for every production result, record one of: source parity implemented, deliberately transform-only with a focused test, or unrelated type/import reference. The focused suites below are evidence; do not manually classify every test reference. First syntax-check this exact command block with `bash -n`, then execute it:

```bash
bash -n <<'BASH'
rg -n -U "PluginId\(\s*['\"](?:source|transform)['\"]\s*,\s*['\"]llm['\"]\s*\)" src/elspeth
rg -n "PluginCapability\.LLM" src/elspeth
rg -n "get_source_by_name\([^\n]*llm|get_transform_by_name\([^\n]*llm" src/elspeth
rg -n "LLMSource|LLMTransform" src/elspeth
rg -n "composition_has_llm" src/elspeth
rg -n "llm_source|llm_node|llm_transform" src/elspeth
rg -n "(plugin\s*(==|!=)\s*['\"]llm['\"]|['\"]llm['\"]\s*(==|!=)\s*plugin)" src/elspeth --glob '*.py'
rg -n "(plugin\s*(===|!==)\s*['\"]llm['\"]|['\"]llm['\"]\s*(===|!==)\s*plugin)" src/elspeth/web/frontend/src --glob '*.ts' --glob '*.tsx'
rg -n "isLlmNode|isLlmSource" src/elspeth/web/frontend/src --glob '*.ts' --glob '*.tsx'
BASH
rg -n -U "PluginId\(\s*['\"](?:source|transform)['\"]\s*,\s*['\"]llm['\"]\s*\)" src/elspeth
rg -n "PluginCapability\.LLM" src/elspeth
rg -n "get_source_by_name\([^\n]*llm|get_transform_by_name\([^\n]*llm" src/elspeth
rg -n "LLMSource|LLMTransform" src/elspeth
rg -n "composition_has_llm" src/elspeth
rg -n "llm_source|llm_node|llm_transform" src/elspeth
rg -n "(plugin\s*(==|!=)\s*['\"]llm['\"]|['\"]llm['\"]\s*(==|!=)\s*plugin)" src/elspeth --glob '*.py'
rg -n "(plugin\s*(===|!==)\s*['\"]llm['\"]|['\"]llm['\"]\s*(===|!==)\s*plugin)" src/elspeth/web/frontend/src --glob '*.ts' --glob '*.tsx'
rg -n "isLlmNode|isLlmSource" src/elspeth/web/frontend/src --glob '*.ts' --glob '*.tsx'
```

- [ ] **Discovery/identity:** fresh-process discovery, nested scan, shared-manager identity, no duplicates, plugin-hash directory inventory 38.
- [ ] **Profiles/runtime/value sources:** plural sources only, key `source` plus arbitrary names, second-pass idempotence, audit alias provenance, strict-config stripping, server/user/scope-less credentials, invalid OpenRouter/Azure, declaration-free Bedrock/Gateway.
- [ ] **Coverage/alternate exits/auto-wire:** component type, direct-sink rejection, success post-domination, every non-discard validation exit rejected under REQUIRED, discard accepted, transform-only Prompt Shield, singular/plural Composer provenance, ambiguous dual no-op, collision/budget/mixed/idempotence.
- [ ] **Execution/readiness/catalogue/authoring:** base URL, tracing, profile and control gates, transform-only retry/interpretation, source boundary prose, public source schema, source-only planner aids, stale advisory, live goldens, all provider availability modes.
- [ ] **Consent:** current/failed/stale/not-yet-loaded/unknown catalogue states; authored alias only, safe authored model fallback, generic fallback, no private resolved model, ordinary sources unchanged.
- [ ] **Provider/Landscape exact matrix:** Azure and Bedrock each one logical LLM row; OpenRouter and Gateway each one HTTP transport plus one logical LLM row; same real operation ID, `ERROR` on failures, no pre-yield state/token, exactly one source row at index 0.
- [ ] **Resume/usage/cleanup:** exhausted not reopened, interrupted resume refused, known/partial/unknown/inconsistent usage exact, cleanup cannot replace the primary outcome, cleanup telemetry is operation-correlated after load.
- [ ] **Telemetry/log sentinel capture:** inject unique prompt, response, provider-body, credential, and secret-reference sentinels; assert none appears in `ResourceCleanupFailed`, console telemetry, or captured logs. Keep cleanup output bounded to resource/error type/correlation metadata.

Run focused Python suites, including the integration surfaces omitted by narrower tasks:

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/sources/llm \
  tests/unit/plugins/llm \
  tests/integration/plugins/llm \
  tests/unit/plugins/test_discovery.py \
  tests/unit/plugins/test_builtin_plugin_metadata.py \
  tests/unit/core/test_llm_profile_catalog.py \
  tests/unit/web/plugin_policy \
  tests/unit/web/composer/test_required_control_autowire.py \
  tests/integration/web/composer/guided/test_shared_planner_surfaces.py \
  tests/unit/web/execution \
  tests/integration/web/test_execute_pipeline.py \
  tests/unit/web/audit_readiness \
  tests/unit/web/catalog \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/e2e/examples/test_shipped_examples.py -q
```

Run the focused frontend test and the complete Vitest suite, restoring cwd each time:

```bash
(cd src/elspeth/web/frontend && npm test -- src/components/sidebar/ExecuteButton.test.tsx)
(cd src/elspeth/web/frontend && npm test)
(cd src/elspeth/web/frontend && npm run typecheck && npm run lint)
```

- [ ] Run complete Python/static checks up to, but not including, the full lint:

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m mypy src/elspeth
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
PYTHONPATH=src:elspeth-lints/src .venv/bin/python scripts/wardline_gate.py
```

Wardline must exit 0 and be non-inert.

- [ ] As the final source-file modification, mechanically recompute/apply both plugin hashes, then independently verify both declarations match recomputation before running the full lint:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.cicd.plugin_hash import compute_source_file_hash, extract_plugin_attributes, fix_source_file_hash

targets = (
    (Path("src/elspeth/plugins/sources/llm/source.py"), "LLMSource"),
    (Path("src/elspeth/plugins/transforms/llm/transform.py"), "LLMTransform"),
)
for path, class_name in targets:
    fix_source_file_hash(path, class_name, compute_source_file_hash(path))
for path, class_name in targets:
    expected = compute_source_file_hash(path)
    declared = {item.class_name: item.source_file_hash for item in extract_plugin_attributes(path)}[class_name]
    if declared != expected:
        raise SystemExit(f"hash mismatch for {class_name}: declared={declared} expected={expected}")
PY
PYTHONPATH=src:elspeth-lints/src .venv/bin/elspeth-lints check --rules plugin_contract.plugin_hashes
PYTHONPATH=src:elspeth-lints/src .venv/bin/elspeth-lints check
```

Preferred full-lint result is exit 0. If and only if its sole failure is the pre-existing operator-held judge-signature condition, prove the branch adds no signature drift and no other lint finding, record that evidence, and allow feature closeout without forbidden signing. Never stage, sign, rotate, rekey, or hand-edit judge metadata.

- [ ] Inspect final state:

```bash
git diff --check
git status --short
git diff release/0.7.2...HEAD --stat
git log --oneline release/0.7.2..HEAD
```

- [ ] Commit a final mechanical hash-only refresh if Task 12 changed either declaration; create no empty closeout commit.
- [ ] Record exact evidence on `elspeth-b35b10722c` and `elspeth-0b075bdf2b`. Only now, after every focused/full check and required-control parity surface passes, close the P1 and then the feature.
- [ ] Leave the worktree clean. Do not push or merge.
