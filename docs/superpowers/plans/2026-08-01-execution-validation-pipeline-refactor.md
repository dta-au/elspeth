# Execution Validation Pipeline Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair two confirmed validation-ledger defects and refactor web execution validation into typed ordered phases with one ledger owner.

**Architecture:** Keep `elspeth.web.execution.validation` as the compatibility facade and call-time dependency source. Introduce typed artifact carriers, concrete phase modules, and a ledger that owns check ordering, failure cascades, and terminal result construction. Migrate one phase family at a time on sequential branches so public differential tests remain the oracle.

**Tech Stack:** Python 3.13, dataclasses, pydantic response models, pytest, Ruff/mypy through repository gates, Loomweave, Filigree, Wardline.

**Status (2026-08-01):** Tasks 1–8 are implemented locally through integration
commit `6c4d843c7` on `codex/execution-validation-pipeline`; trust and house-style
closeout continues from `1a2fda249` on
`codex/execution-validation-trust-cleanup`. Task 9 remains open for final
integration testing and tracker closeout.

**Delivered deviations:** The implementation did not add the proposed
`ValidationRequest` carrier; public arguments remain explicit and only injected
functions are grouped in `ValidationDependencies`. Phase termination uses
`PhaseReport.apply()` / `PhaseFailure.apply()` plus one `PhaseTermination` catch
in the runner. The touched-file trust review removed signature-churn workarounds
and records three explicit nominal R5 adjudication candidates in the scanner
regression; it did not edit or sign the tier-model allowlist.

---

## Branch order

Every task uses a fresh branch and `.claude/worktrees/` worktree. A task is
merged into `codex/execution-validation-pipeline` only after a specification
review, code-quality review, focused verification, and primary-agent diff
inspection. Each later branch starts from the updated integration branch.

### Task 1: Preserve policy passes on path failure

**Branch:** `codex/execution-validation-bug-path-ledger`
**Files:**
- Modify: `src/elspeth/web/execution/validation.py:1015-1148`
- Modify: `tests/unit/web/execution/test_validation.py`
- Modify: `tests/unit/web/execution/test_preflight_side_effects.py:413-426`

- [x] **Step 1: Add a complete-failure-ledger assertion helper**

Add beside `_check()` in `test_validation.py`:

```python
def _assert_complete_failure_ledger(result: Any, failed_name: str) -> None:
    from elspeth.web.execution.schemas import VALIDATION_BLOCKING_CHECK_NAMES

    assert [check.name for check in result.checks] == list(VALIDATION_BLOCKING_CHECK_NAMES)
    failed_index = VALIDATION_BLOCKING_CHECK_NAMES.index(failed_name)
    assert all(check.passed for check in result.checks[:failed_index])
    assert result.checks[failed_index].passed is False
    assert result.checks[failed_index].outcome_code is None
    assert all(
        check.passed is False and check.outcome_code == CHECK_OUTCOME_SKIPPED_AFTER_FAILURE
        for check in result.checks[failed_index + 1 :]
    )
```

- [x] **Step 2: Add a failing regression for all three path families**

Create a parametrized test covering source `path`, sink `path`, and nested
transform `provider_config.persist_directory`. Each value must resolve outside
`/tmp/test_data`; call `validate_pipeline_for_trained_operator()` and assert:

```python
assert result.is_valid is False
_assert_complete_failure_ledger(result, "path_allowlist")
```

Change `test_validate_pipeline_rejects_chroma_persist_directory_outside_data_dir`
to locate the path check by name rather than asserting it is `checks[0]`:

```python
path_check = next(check for check in result.checks if check.name == "path_allowlist")
assert path_check.passed is False
assert "persist_directory" in path_check.detail
```

- [x] **Step 3: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py -k 'path_failure_preserves' -q
```

Expected: all parametrized cases fail because the first emitted name is
`path_allowlist`, not `plugin_enablement`.

- [x] **Step 4: Preserve the accumulated list in each path branch**

For source, sink, and nested-transform path failures, replace the fresh-list
return with this sequence:

```python
checks.append(
    ValidationCheck(
        name=_CHECK_PATH_ALLOWLIST,
        passed=False,
        detail=detail,
        affected_nodes=affected_nodes,
        outcome_code=None,
    )
)
_append_skipped_checks(checks, _CHECK_PATH_ALLOWLIST)
return ValidationResult(
    is_valid=False,
    checks=checks,
    errors=[error],
    readiness=readiness,
)
```

Retain each branch's existing detail, affected nodes, error, and readiness
objects exactly; only list ownership changes.

- [x] **Step 5: Run GREEN and neighboring tests**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_preflight_side_effects.py -q
```

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add src/elspeth/web/execution/validation.py tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_preflight_side_effects.py
git commit -m "fix(web): preserve policy checks on path rejection"
```

### Task 2: Complete the schema-failure cascade

**Branch:** `codex/execution-validation-bug-schema-cascade`
**Files:**
- Modify: `src/elspeth/web/execution/validation.py:2493-2558`
- Modify: `tests/unit/web/execution/test_validation.py:4594-4634`

- [x] **Step 1: Extend the schema failure regression**

After the existing schema assertions, add:

```python
_assert_complete_failure_ledger(result, "schema_compatibility")
assert [check.name for check in result.checks[-3:]] == [
    "state_exists",
    "advisor_signoff",
    "proof_diagnostics",
]
```

- [x] **Step 2: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py::TestValidatePipelineRuntimeCheckBoundaries::test_schema_failure_uses_schema_check -q
```

Expected: fail because the last three skipped records are absent.

- [x] **Step 3: Append the canonical skipped tail**

Immediately before the schema-failure `ValidationResult`, add:

```python
_append_skipped_checks(checks, _CHECK_SCHEMA)
```

Do not change error formatting, component attribution, or readiness.

- [x] **Step 4: Run GREEN and the validation module**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py -q
```

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/elspeth/web/execution/validation.py tests/unit/web/execution/test_validation.py
git commit -m "fix(web): complete schema validation cascade"
```

### Task 3: Introduce typed carriers and the ledger

**Branch:** `codex/execution-validation-refactor-ledger`
**Files:**
- Create: `src/elspeth/web/execution/_validation_model.py`
- Create: `src/elspeth/web/execution/_validation_ledger.py`
- Create: `tests/unit/web/execution/test_validation_ledger.py`

- [x] **Step 1: Write failing ledger unit tests**

Test the following public-internal behaviors through `ValidationLedger`:

```python
ledger = ValidationLedger()
ledger.record_pass(_check("plugin_enablement", passed=True))
ledger.record_pass(_check("operator_profile_options", passed=True))
result = ledger.finish_failure(
    _check("required_control_availability", passed=False),
    errors=(_error("blocked"),),
    readiness=_blocked_readiness(),
)
assert [check.name for check in result.checks] == list(VALIDATION_BLOCKING_CHECK_NAMES)
assert result.checks[0].passed is True
assert result.checks[1].passed is True
assert result.checks[2].passed is False
assert all(
    check.outcome_code == CHECK_OUTCOME_SKIPPED_AFTER_FAILURE
    for check in result.checks[3:]
)
```

Also assert duplicate core names, out-of-order core names, a pass after failure,
and an advisory before all 24 core passes raise `RuntimeError`. Assert that
empty-state and outer-layer synthetic shapes are not model-level ledger inputs.

- [x] **Step 2: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_ledger.py -q
```

Expected: import failure because the new modules do not exist.

- [x] **Step 3: Add carrier types**

Define frozen, slotted dataclasses in `_validation_model.py`:

```python
@dataclass(frozen=True, slots=True)
class PolicyLoweredState:
    state: CompositionState
    operator_resolved_model_node_ids: frozenset[str]

@dataclass(frozen=True, slots=True)
class AuthoredValidatedState:
    policy: PolicyLoweredState
    all_secret_refs: tuple[tuple[str, SecretScope | None], ...]
    env_ref_names: frozenset[str]
    semantic_contracts: tuple[SemanticEdgeContractResponse, ...]

@dataclass(frozen=True, slots=True)
class MaterializedYaml:
    authored: AuthoredValidatedState
    materialized_state: CompositionState
    pipeline_yaml: str
```

Add `LoadedRuntime`, `InstantiatedRuntime`, and `GraphedRuntime` with concrete
settings, bundle, graph, and ordered graph-warning fields. Keep engine-only
types behind `TYPE_CHECKING` where importing them would create cycles.

- [x] **Step 4: Implement `ValidationLedger`**

Use `VALIDATION_BLOCKING_CHECK_NAMES` as the sole order authority. Define the
24-name core prefix ending at `schema_compatibility`; `finish_failure()` emits
the full canonical skipped suffix, while `finish_success()` requires all 24
core names and permits zero or more `identity_node_advisory` records.

- [x] **Step 5: Run GREEN and type/lint checks**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_ledger.py -q
.venv/bin/ruff check src/elspeth/web/execution/_validation_model.py src/elspeth/web/execution/_validation_ledger.py tests/unit/web/execution/test_validation_ledger.py
.venv/bin/mypy src/elspeth/web/execution/_validation_model.py src/elspeth/web/execution/_validation_ledger.py
```

Expected: pass.

- [x] **Step 6: Commit**

```bash
git add src/elspeth/web/execution/_validation_model.py src/elspeth/web/execution/_validation_ledger.py tests/unit/web/execution/test_validation_ledger.py
git commit -m "refactor(web): add typed validation ledger"
```

### Task 4: Wire the compatibility facade and runner skeleton

**Branch:** `codex/execution-validation-refactor-runner`
**Files:**
- Create: `src/elspeth/web/execution/_validation_pipeline.py`
- Modify: `src/elspeth/web/execution/validation.py`
- Modify: `tests/unit/web/execution/test_validation.py`
- Modify: `tests/integration/web/test_plugin_policy_end_to_end.py`

- [x] **Step 1: Add characterization tests for facade identity**

Pin exact `inspect.signature(validate_pipeline)`, wrapper forwarding, and the
five facade patch targets. For each injected runtime function, patch the name
on `elspeth.web.execution.validation` and assert the patched function is called
through the public entry point.

- [x] **Step 2: Run RED for the requested runner surface**

Add a test importing `ValidationPipeline` and constructing it from an explicit
`ValidationDependencies` dataclass. The test must fail before the module exists.

- [x] **Step 3: Implement call-time dependency capture**

In `validation.py`, build `ValidationDependencies` inside `validate_pipeline()`
from current module globals:

```python
dependencies = ValidationDependencies(
    load_yaml=load_bounded_pipeline_yaml,
    load_settings_yaml=load_settings_from_yaml_string,
    load_settings_dict=load_settings_from_config_dict,
    instantiate_plugins=instantiate_runtime_plugins,
    build_graph=build_runtime_graph,
    validate_routes=assemble_and_validate_pipeline_config,
)
```

Delegate to `ValidationPipeline(dependencies).run(...)`. Keep the observation
decorator and exact public signature on the facade function.

- [x] **Step 4: Keep behavior delegated to the legacy body initially**

Move the existing body behind one runner method without changing stage order or
`try` scopes. This commit establishes injection and ownership only; it must not
mix phase extraction or behavior changes.

- [x] **Step 5: Verify**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py tests/integration/web/test_plugin_policy_end_to_end.py -q
```

Expected: pass with all existing facade patches still effective.

- [x] **Step 6: Commit**

```bash
git add src/elspeth/web/execution/_validation_pipeline.py src/elspeth/web/execution/validation.py tests/unit/web/execution/test_validation.py tests/integration/web/test_plugin_policy_end_to_end.py
git commit -m "refactor(web): introduce execution validation runner"
```

### Task 5: Extract authored-state phases

**Branch:** `codex/execution-validation-refactor-authoring`
**Files:**
- Create: `src/elspeth/web/execution/_validation_authoring.py`
- Modify: `src/elspeth/web/execution/_validation_pipeline.py`
- Modify: `tests/unit/web/execution/test_validation.py`
- Create: `tests/unit/web/execution/test_validation_authoring.py`

- [x] **Step 1: Write phase tests first**

Test policy lowering, source/sink/nested path failures, web network/resource
failures, secret evidence, semantic evidence, batch validation, and pending
interpretation. Each test asserts a typed carrier or `PhaseFailure`, never a
mock call to a private helper.

- [x] **Step 2: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_authoring.py -q
```

Expected: import failures for the new phase functions.

- [x] **Step 3: Move authored checks in canonical order**

Create concrete functions returning `PhaseReport[T]`. They do not mutate the
ledger. The runner applies reports and terminates on the first `PhaseFailure`.
Retain the empty-pipeline short circuit in the facade/runner as an explicit
legacy producer shape.

- [x] **Step 4: Run public differential tests**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_authoring.py tests/unit/web/execution/test_validation.py -q
```

Expected: pass.

- [x] **Step 5: Commit**

```bash
git add src/elspeth/web/execution/_validation_authoring.py src/elspeth/web/execution/_validation_pipeline.py tests/unit/web/execution/test_validation_authoring.py tests/unit/web/execution/test_validation.py
git commit -m "refactor(web): extract authored validation phases"
```

### Task 6: Extract materialization and provider-policy phases

**Branch:** `codex/execution-validation-refactor-materialization`
**Files:**
- Create: `src/elspeth/web/execution/_validation_materialization.py`
- Modify: `src/elspeth/web/execution/_validation_pipeline.py`
- Create: `tests/unit/web/execution/test_validation_materialization.py`
- Modify: `tests/unit/web/execution/test_validate_blob_inline.py`

- [x] **Step 1: Write phase tests first**

Cover YAML/path materialization, metadata-only blob validation and substitution,
managed identity, LLM retry/base URL/tracing, S3 endpoint, and S3-source policy.
Assert that provider checks execute after the blob check even though they do not
consume its data.

- [x] **Step 2: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_materialization.py -q
```

Expected: import failures.

- [x] **Step 3: Extract without broadening exception catches**

YAML generation and path-resolution exceptions remain uncaught. Blob
substitution produces the exact YAML consumed by settings loading. Schema
diagnostics continue to retain the policy-lowered state separately from the
materialized state.

- [x] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_materialization.py tests/unit/web/execution/test_validate_blob_inline.py tests/unit/web/execution/test_validation.py -q
git add src/elspeth/web/execution/_validation_materialization.py src/elspeth/web/execution/_validation_pipeline.py tests/unit/web/execution/test_validation_materialization.py tests/unit/web/execution/test_validate_blob_inline.py tests/unit/web/execution/test_validation.py
git commit -m "refactor(web): extract validation materialization phases"
```

### Task 7: Extract runtime admission phases

**Branch:** `codex/execution-validation-refactor-runtime`
**Files:**
- Create: `src/elspeth/web/execution/_validation_runtime.py`
- Modify: `src/elspeth/web/execution/_validation_pipeline.py`
- Create: `tests/unit/web/execution/test_validation_runtime.py`
- Modify: `tests/unit/web/execution/test_validation.py`
- Modify: `tests/unit/web/execution/test_validation_value_source.py`

- [x] **Step 1: Write phase tests first**

Cover settings typed catches and missing-part reframing, plugin/value-source
exception discrimination, graph warnings, graph errors, route errors, schema
errors, rich `EdgeContractError` formatting, and unexpected exception
propagation.

- [x] **Step 2: Run RED**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_runtime.py -q
```

Expected: import failures.

- [x] **Step 3: Extract with exact stage-local catches**

Keep four separate catches:

```python
except (PydanticValidationError, ValueError, TypeError): ...
except (ValueSourceValidationError, PluginNotFoundError, PluginConfigError, FileExistsError): ...
except GraphValidationError: ...
except RouteValidationError: ...
```

The schema phase has its own `GraphValidationError` catch. Do not add a
runner-wide `except Exception`.

- [x] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation_runtime.py tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_validation_value_source.py tests/integration/pipeline/test_composer_runtime_agreement.py -q
git add src/elspeth/web/execution/_validation_runtime.py src/elspeth/web/execution/_validation_pipeline.py tests/unit/web/execution/test_validation_runtime.py tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_validation_value_source.py
git commit -m "refactor(web): extract runtime validation phases"
```

### Task 8: Extract diagnostics and preserve facade exports

**Branch:** `codex/execution-validation-refactor-diagnostics`
**Files:**
- Create: `src/elspeth/web/execution/_validation_diagnostics.py`
- Modify: `src/elspeth/web/execution/validation.py`
- Modify: `src/elspeth/web/execution/_validation_runtime.py`
- Modify: `tests/unit/web/execution/test_validation.py`
- Modify: `tests/unit/web/execution/test_identity_node_advisory.py`

- [x] **Step 1: Add import-compatibility tests**

Pin existing imports of `_build_edge_contract_suggestion`,
`_format_edge_contract_failure`, `_collect_secret_refs`,
`_infer_component_type_from_plugin_error`, and
`_reframe_settings_missing_parts` from the facade module.

- [x] **Step 2: Run RED for the diagnostics module**

Add direct behavior tests importing the same implementations from
`_validation_diagnostics`; expect import failure before creation.

- [x] **Step 3: Move diagnostic logic and re-export**

Move edge-contract formatting, blob/settings reframing, and identity detection.
Import those names into `validation.py` so existing imports continue to resolve.
Do not change output prose or ordering.

- [x] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_identity_node_advisory.py -q
git add src/elspeth/web/execution/_validation_diagnostics.py src/elspeth/web/execution/validation.py src/elspeth/web/execution/_validation_runtime.py tests/unit/web/execution/test_validation.py tests/unit/web/execution/test_identity_node_advisory.py
git commit -m "refactor(web): isolate validation diagnostics"
```

### Task 9: Integrated verification and closeout

**Branch:** `codex/execution-validation-pipeline`

- [ ] **Step 1: Re-audit the merged diff**

```bash
git diff release/0.7.2...HEAD --check
git diff --stat release/0.7.2...HEAD
```

Confirm no Composer state implementation, signed allowlist, generated artifact,
or unrelated acceptance file changed.

- [ ] **Step 2: Run focused web validation tests**

```bash
.venv/bin/pytest tests/unit/web/execution tests/integration/web/test_plugin_policy_end_to_end.py tests/integration/pipeline/test_composer_runtime_agreement.py -q
```

- [ ] **Step 3: Run repository gates**

```bash
.venv/bin/pytest tests/
.venv/bin/elspeth-lints check
.venv/bin/python scripts/wardline_gate.py
```

The known operator-signature trust-tier state may remain fail-closed as
documented by the repository; no agent signs or rekeys it.

- [ ] **Step 4: Update Filigree and summarize**

Attach the final integration commit to `elspeth-39d6d479c0`, describe the two
repaired defects, list every merged branch/commit, record exact gate outcomes,
and close only if the entire planned scope is complete.
