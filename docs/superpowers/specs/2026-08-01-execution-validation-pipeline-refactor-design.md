# Execution Validation Pipeline Refactor Design

**Status:** Approved for implementation by the operator on 2026-08-01
**Tracking:** `elspeth-39d6d479c0`
**Integration branch:** `codex/execution-validation-pipeline`

## Purpose

Refactor `elspeth.web.execution.validation` so that validation order, failure
cascades, and phase dependencies are explicit and independently testable. The
work is intended to expose and repair latent validation-ledger defects while
preserving the production admission path and every public API contract.

The refactor must not touch Composer state implementation. Another workstream
owns that surface. `CompositionState` remains an input contract only.

## Current problem

`validate_pipeline()` is a roughly 1,750-line function. It combines:

- authored-state plugin and resource policy;
- source, sink, and nested-path confinement;
- secret reference and credential-placement checks;
- semantic, batch, and interpretation review checks;
- YAML materialization and inline-blob validation;
- provider-specific web policy;
- settings loading and secret resolution;
- plugin instantiation and value-source compliance;
- graph construction, route-target validation, and edge compatibility; and
- construction of the ordered `ValidationCheck` audit ledger.

Most failure branches manually append a failed check, construct errors and
readiness, append skipped checks, and return. Small differences between those
branches create observable audit defects. One confirmed example is the path
allowlist failure family: those branches replace the accumulated check list,
discarding successful plugin-policy records that ran earlier.

## Observable contract

The following are load-bearing and remain unchanged except for separately
tested defect corrections:

1. Public call signatures for `validate_pipeline()` and
   `validate_pipeline_for_trained_operator()`.
2. Strict `ValidationResult`, `ValidationCheck`, `ValidationError`,
   `ValidationWarning`, readiness, and semantic-contract schemas.
3. The canonical order in `VALIDATION_BLOCKING_CHECK_NAMES`.
4. Exactly one terminal record for each blocking check: passed, failed, or
   `validation.skipped_after_failure` when the caller requests a complete
   failure ledger.
5. Existing detail text, error codes, component attribution, suggestions,
   warnings, and readiness axes unless a regression test demonstrates a defect.
6. Typed exception handling. Expected Tier-3/configuration failures become
   `ValidationResult`; unknown exceptions and Tier-1 invariant failures still
   propagate.
7. Runtime parity: settings loading, plugin construction, graph construction,
   route validation, and schema validation continue to call the same engine
   functions as execution.
8. Patch/import seams used by callers and tests remain available from
   `elspeth.web.execution.validation` during this refactor.

## Approaches considered

### 1. Mechanical helper extraction

Move consecutive blocks into functions returning `ValidationResult | None`
but retain manual ledger construction in every helper.

This has the lowest immediate code-motion risk but preserves the defect class:
each helper can still omit prior checks or downstream skipped records. It also
does not create one inspectable execution model. Rejected as insufficient.

### 2. Generic declarative rule engine

Represent every check as registry data with dynamic dependencies, generic
inputs, and a universal evaluator.

This would maximize uniformity but introduces a second validation framework,
weakens concrete typing across materially different phases, and creates a
large simultaneous migration. Rejected as unnecessary abstraction.

### 3. Typed incremental pipeline with one ledger owner

Introduce a small internal ledger/result builder and extract concrete ordered
phase functions. Each phase continues to use domain-specific types; the runner
alone owns ordering, prior-result preservation, skipped-check completion, and
terminal `ValidationResult` construction.

This is the selected approach. It removes the repeated failure choreography
without hiding the policy inside a generic rules language, and it permits one
phase to be migrated and verified at a time.

## Architecture

### Validation ledger

Create `elspeth.web.execution._validation_ledger` containing a private
`ValidationLedger` type. It owns:

- the accumulated `ValidationCheck` and `ValidationError` collections;
- canonical check-order validation;
- appending passed and failed checks;
- producing non-duplicated skipped records after a failed check; and
- constructing a failed `ValidationResult` with readiness and optional
  semantic-contract evidence.

The ledger is internal. No new HTTP or MCP response fields are introduced.

`validate_pipeline()` directly executes the first 24 blocking checks, through
`schema_compatibility`. `state_exists`, `advisor_signoff`, and
`proof_diagnostics` belong to outer Composer/workflow stages. Successful core
validation therefore still returns 24 passed records. A core failure retains
the established cross-layer projection by marking every later name in the
full 27-name canonical sequence skipped, including those three outer checks.

The ledger must fail loudly if a phase attempts to emit a core blocking check
before an earlier core check has a terminal record, emits a duplicate blocking
check, or emits a check outside the declared vocabulary. Explicit producer
shapes such as the empty-pipeline short circuit and outer-layer synthetic
results remain producer-specific rather than being rejected by a universal
`ValidationResult` model validator. Advisory checks remain allowed only after
all runtime blocking phases succeed.

### Pipeline contexts

Use explicit typed carriers rather than a single untyped mutable bag:

- `ValidationRequest`: immutable caller inputs and injected runtime functions;
- `PolicyLoweredState`: executable state plus operator-resolved model IDs;
- `AuthoredValidatedState`: secret inventory and semantic evidence;
- `MaterializedYaml`: materialized state and the exact runtime YAML;
- `LoadedRuntime`: parsed settings;
- `InstantiatedRuntime`: settings plus the runtime plugin bundle; and
- `GraphedRuntime`: bundle, graph, and graph warnings.

Transitions construct the next carrier only after prerequisites succeed. A
phase therefore cannot accidentally read a graph before graph construction,
resolved settings before settings loading, or blob-substituted YAML before
inline-blob validation.

Schema diagnostics and identity advisories continue to use the policy-lowered
state, not the interpretation-materialized state. Settings loading retains the
secret inventory produced by authored validation. Graph warnings remain
visible only on overall success, matching the current response contract.

### Ordered phases

The runner executes these concrete phases:

1. Empty-state admission and plugin-policy lowering.
2. Authored resource policy: paths, web network/resource limits, and secrets.
3. Authored semantic policy: semantic contracts, batch options, and
   interpretation review.
4. Materialization: YAML generation, inline blobs, managed identity, LLM
   policy, and AWS S3 policy.
5. Runtime construction: settings loading, plugin instantiation, and
   value-source compliance.
6. Runtime graph admission: structure, route targets, and schema compatibility.
7. Successful-path advisories and final result.

The canonical individual check order remains defined by
`VALIDATION_BLOCKING_CHECK_NAMES`; phase grouping does not create a second
order source.

### Compatibility facade

`elspeth.web.execution.validation` remains the public module. It retains the
observation-boundary decorator, exact public signatures, public wrappers,
compatibility exports needed by existing tests, and dependency wiring. Runtime
functions are resolved from the facade at call time and injected into the
runner, so patches against `validation.load_settings_from_yaml_string`,
`validation.load_settings_from_config_dict`,
`validation.instantiate_runtime_plugins`, `validation.build_runtime_graph`, and
`validation.assemble_and_validate_pipeline_config` continue to affect the
exercised code.

Existing stage-local `try` blocks remain stage-local. YAML generation, path
resolution, policy validation, semantic validation, and dependency-carrier
construction must not be moved beneath broader catches. Unknown exceptions and
Tier-1 invariant failures continue to propagate.

## Defect handling

Defects discovered during characterization are not folded invisibly into
mechanical refactor commits.

Each defect receives:

1. an isolated branch and worktree;
2. a failing public-behavior regression test;
3. recorded red output;
4. the smallest production correction;
5. focused green output and relevant neighboring tests;
6. specification and code-quality review; and
7. a separate merge into the integration branch.

The integration history must distinguish behavior corrections from structural
movement.

## Branch and merge model

- `codex/execution-validation-pipeline` is the integration branch.
- Confirmed defects use `codex/execution-validation-bug-*` branches.
- Structural work uses `codex/execution-validation-refactor-*` branches based
  on the integration branch after defect merges.
- Each branch has a dedicated `.claude/worktrees/` worktree and the mandated
  symlink to the main checkout's `.venv`.
- Subagents edit only their assigned worktree. Reviews are read-only.
- The primary agent independently checks diffs and reruns verification before
  each local merge.
- No push or pull request is part of this task.

## Testing strategy

### Ledger invariants

Add focused unit tests for:

- preserving all prior terminal check records on failure;
- one failed check followed by every later check exactly once as skipped;
- no passed check after an earlier failure;
- duplicate and out-of-order emissions failing closed; and
- advisory checks occurring only on the success path.

### Public differential tests

Exercise `validate_pipeline_for_trained_operator()` and the web-principal path
at representative failure points. Assertions cover the entire ordered check
projection and the existing errors, readiness, warnings, and semantic evidence,
not private helper calls.

### Runtime parity

Retain tests proving that the real settings loader, runtime plugin factory,
graph builder, route-target validator, and edge-compatibility validator are
called in the expected order with the expected artifacts.

### Gates

Each defect branch runs its new regression plus
`tests/unit/web/execution/test_validation.py`. The refactor branch runs all
unit and integration tests that import the validation facade. The merged
integration branch runs:

```bash
.venv/bin/pytest tests/
.venv/bin/elspeth-lints check
.venv/bin/python scripts/wardline_gate.py
```

Wardline is required because this is a web-facing trust-boundary pipeline.
The known operator-held trust-tier signing stage remains outside this task.

## Non-goals

- No changes to Composer state validation or authoring semantics.
- No response-schema expansion or new public telemetry format.
- No generic validation framework for unrelated subsystems.
- No global trust-tier allowlist signing, rekeying, or baseline repair.
- No deployment, backend restart, push, or pull request.

## Success criteria

The work is complete when confirmed ledger defects have isolated regression
tests and fixes, `validate_pipeline()` is an orchestration facade over explicit
typed phases with one ledger owner, the observable contract is preserved, all
required gates have fresh evidence, and the verified branches have been merged
locally into `codex/execution-validation-pipeline`.
