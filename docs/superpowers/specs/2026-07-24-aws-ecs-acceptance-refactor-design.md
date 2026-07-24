# AWS ECS Acceptance Controller Refactor Design

**Status:** Approved for `release/0.7.2`
**Date:** 2026-07-24
**Decision owner:** Project developer

## Summary

Refactor `src/elspeth/web/aws_ecs_acceptance.py` into focused private modules while preserving the executable module path, command-line interface (CLI), import compatibility, security behavior, receipt and manifest semantics, and operational outputs.

This refactor is part of 0.7.2. Its work ends when the refactored source is complete, locally verified, frozen, and handed back to the release owner. Signing, generated release metadata, release CI, and landing happen out of band and are not steps in this design.

## Current state

At the 2026-07-24 design snapshot, the controller has:

- 9,965 source lines;
- 4,923 statements and 2,150 branches;
- 278 controller unit tests and 43 runbook-contract tests;
- 24 top-level commands and 36 dispatch leaves;
- 55 tracked runbook invocation sites; and
- 73.123144% branch-inclusive controller coverage in the focused controller/runbook lane.

These values establish why the split is necessary. They are not permanent acceptance literals. The implementation preflight must recapture the exact counts and coverage from the selected green base because 0.7.2 is still moving.

## Goals

- Keep `python -m elspeth.web.aws_ecs_acceptance COMMAND [OPTIONS]` stable.
- Keep existing importable public symbols available from `elspeth.web.aws_ecs_acceptance` by identity.
- Give each security, validation, AWS, persistence, and orchestration concern one clear owner.
- Preserve behavior while moving code; do not combine unrelated fixes with extraction commits.
- Keep every extraction commit independently testable and revertible.
- Finish all source movement and hand back one locally verified, frozen source commit.

## Non-goals

- Do not redesign the CLI, output schemas, error envelopes, receipt schemas, approval semantics, retry budgets, or cleanup order.
- Do not certify or execute the exhaustive disposable-environment AWS runbook.
- Do not deploy or mutate the live ECS service as part of this refactor.
- Do not create a reusable acceptance-controller framework.
- Do not absorb unrelated release defects into extraction commits.
- Do not create signed plans, review receipts, per-task approval records, or one Filigree child for every mechanical move.
- Do not stage or apply Judge bundles, generate signature or fingerprint metadata, run release-candidate CI, push release branches, or land the frozen source. The release owner handles those activities out of band.

## Compatibility contract

The permanent facade remains `src/elspeth/web/aws_ecs_acceptance.py`. It owns:

- `build_parser()`;
- `main()`;
- JSON/error/stdout helpers;
- the `__main__` guard; and
- compatibility re-exports for every currently defined public symbol.

Before moving production code, a tests-only commit must characterize:

- the 24 top-level commands and all nested parser actions;
- the 36 dispatch leaves and their argument conversions;
- mutually exclusive and required argument groups;
- direct public-symbol imports and re-export identity;
- the executable module path;
- exact safe stdout/stderr behavior;
- static, non-leaking error envelopes for expected and unexpected exceptions; and
- the deterministic `scenario-namespace` probe.

Use explicit contract tables for commands, dispatch targets, and public exports. Generate no review sidecar. Compare final collection and coverage with values captured from the selected base rather than preserving dated fixture counts.

## Target package

Create `src/elspeth/web/_aws_ecs_acceptance/` with a side-effect-free `__init__.py` and these modules:

| Module | Responsibility |
|---|---|
| `contracts.py` | Shared errors, closed field sets, budgets, identity/hash/time helpers, AWS region validation, namespace helpers, and provider-neutral constants. |
| `secure_documents.py` | Protected reads/writes, parent/destination validation, exact file modes, atomic replacement, receipt-manifest lock creation, cross-process locking, and the serialized mutation decorator. |
| `state.py` | `AcceptanceCredentials`, `AcceptanceState`, state serialization, and state-specific timestamp/error mapping. |
| `http_client.py` | Bounded authenticated HTTP transport and response handling. |
| `capture.py` | Pipeline construction, run/artifact selection, capture, API verification, local authentication, storage provisioning, and payload verification. |
| `receipt_contracts.py` | Exec-receipt encoding/extraction and pure S3, Bedrock, guardrail, telemetry, Terraform, event-canary, compatibility, and stored-receipt validation. |
| `s3.py` | S3 input resolution, effect identity, publication, cleanup, and receipt construction. |
| `bedrock.py` | Bedrock probes, guardrail inputs, plugin-policy acceptance, output suppression, redaction, and live guardrail orchestration. |
| `operator_telemetry.py` | Landscape lifecycle reads, CloudWatch/X-Ray queries, telemetry evidence, outage behavior, metric emission, and connection-budget verification. |
| `manifest_schema.py` | Control-manifest validation/read paths, immutable-finalization guard, and retained-evidence schema validation. |
| `scenario_inventory.py` | Scenario inventory, Terraform/resource bindings, isolation checks, and resolved-value validation. |
| `manifest.py` | Low-level serialized manifest mutations: initialization, scenario binding, retained-evidence binding, operator checkpoints, and read operations. |
| `task_definition.py` | Provider-aware ECS task-definition and Secrets Manager admission checks. |
| `orphan_sweep.py` | AWS client ownership, bounded pagination, ownership projection, and orphan cleanup. |
| `receipt_store.py` | Bounded receipt persistence and `_receipt_store_locked`, with one lock covering receipt publication and manifest indexing. |
| `approvals.py` | Keyring configuration, signature verification, current-approval checks, and serialized approval mutation. |
| `evidence.py` | Evidence sanitization, safe log/value projection, export-receipt construction, and stored-receipt verification. |
| `gate_ledger.py` | Ordered gate journal validation and mutations, candidate binding, replay handling, cleanup records, and finalization. |
| `cleanup.py` | Two-phase cleanup preparation/commit, final cleanup receipts, interruption recovery, and idempotent post-commit verification. |
| `control_service.py` | High-level control-manifest validation/update/load operations that compose manifest, receipt, approval, evidence, and ledger services. |

The facade imports these owners and re-exports compatibility symbols. No private module imports the facade.

## Dependency direction

Dependencies flow downward through five layers:

```text
Layer 0  contracts
             |
Layer 1  secure_documents  state  http_client  receipt_contracts
             |
Layer 2  capture  s3  bedrock  operator_telemetry
         manifest_schema  scenario_inventory  gate_ledger
             |
Layer 3  manifest  task_definition  orphan_sweep  receipt_store  approvals  evidence
             |
Layer 4  cleanup  control_service
             |
Facade   aws_ecs_acceptance
```

An architecture test enforces layer direction and these critical prohibitions:

- no private module imports the facade;
- `s3.py`, `bedrock.py`, and `operator_telemetry.py` do not import one another;
- schema modules do not import mutation services;
- `receipt_store.py` does not import `manifest.py` or `control_service.py`;
- `gate_ledger.py` does not import evidence, cleanup, or control services; and
- `cleanup.py` and `control_service.py` do not import one another.

The test should enforce layers and load-bearing forbidden edges, not freeze every harmless internal import forever.

## Load-bearing invariants

### Serialized protected mutation

`secure_documents.py` owns `_open_receipt_manifest_lock`, `_receipt_manifest_write_lock`, and `_serialized_control_manifest_write`.

Preserve all current properties:

- exact owner and mode `0600`;
- no-follow opens and inode validation;
- hard-link publication of a new lock file;
- `flock` held across the complete read-modify-write transaction;
- temporary-file write, file `fsync`, atomic publication, and parent-directory `fsync`; and
- static error classes without filesystem or secret leakage.

Every manifest, approval, receipt-index, and cleanup mutation retains the serialized decorator or holds the same lock explicitly. Moving the lock inside only the final file write is incorrect because it permits lost updates.

### Immutable finalization

`manifest_schema.py` owns `_require_mutable_control_manifest`. Once `final_evidence.phase == "committed"`, every mutating path fails with the existing `control_manifest_finalized` contract. Post-commit cleanup replay remains validation-only and idempotent.

### Provider-aware task-definition admission

`task_definition.py` preserves the two composer model environment values and resolves both providers. It requires the OpenRouter secret only when either model uses OpenRouter and rejects that secret when it is not required. It also preserves image, role, environment, EFS, runtime-path, secret-selector, and plugin-policy bindings.

### Durable telemetry identity

`operator_telemetry.py` keeps `xray_trace_id(run_id, *, started_at)` bound to the durable Landscape run-start time. Public and existing-run audit adapters continue to load and cache `started_at`; every X-Ray query and retained trace identity uses that value. The refactor must not reintroduce wall-clock-derived trace IDs.

### Receipt and cleanup authority

`receipt_store.py` keeps one lock around receipt publication plus manifest indexing. `cleanup.py` preserves the prepare/commit boundary:

1. Prepare binds receipt aggregates, ledger-prefix identity, and final export without clearing cleanup.
2. Commit requires prepared evidence and verified cleanup surfaces, appends and verifies the terminal cleanup record, commits final hashes, and only then clears `cleanup_required`.
3. Replay after commit verifies the final state without mutation.

## Implementation shape

Use one no-commit preflight and approximately ten independently revertible extraction commits. Do not create a generated release-metadata commit as part of this work.

Move each domain's tests with its production owner instead of postponing the test split until the end. Each extraction commit must leave the facade usable and the branch green.

Recommended milestones:

1. Characterize the facade and add the layer guard.
2. Extract contracts, protected documents, state, and receipt contracts.
3. Extract HTTP/capture and the three AWS/telemetry lanes.
4. Extract manifest schemas, inventories, low-level mutations, and task-definition admission.
5. Extract orphan cleanup, receipt storage, approvals, evidence, and the gate ledger.
6. Extract cleanup/control orchestration and reduce the facade.
7. Run final local acceptance, freeze source, and hand the frozen commit and evidence to the release owner.

Run focused owner tests on every commit. Run the complete controller/runbook/architecture milestone lane after milestones 2, 3, 5, and 6. Do not stack work on a red commit.

## Release and source-freeze strategy

The developer first lands the other intended 0.7.2 changes and declares the release branch ready for structural work. The executor then:

1. resolves the exact remote `release/0.7.2` commit;
2. records it once as `BASE_SHA` and captures its Git tree identity;
3. creates a clean dedicated worktree and implementation branch from that commit;
4. runs the complete preflight baseline before production movement; and
5. prevents unrelated target-branch changes through source freeze and handoff.

If `release/0.7.2` moves before implementation starts, recreate the worktree from the new selected base and repeat preflight. If an authorized source change lands after implementation starts, integrate it before source freeze and rerun the complete local gate. Once the executor records the frozen source commit and tree, any later source change is outside this plan and requires a new verified handoff.

## Verification strategy

Every multi-command gate must fail fast with `set -Eeuo pipefail`, separate executor steps, or explicit status aggregation. A successful final command must never mask an earlier failure.

### Preflight

- Recapture parser, dispatch, public-symbol, test-collection, and coverage baselines from `BASE_SHA`.
- Run the current controller/runbook/architecture lane.
- Run the deterministic scenario probe.
- Run repository Ruff, formatting, mypy, and all locally owned source-sensitive static gates that do not depend on out-of-band release metadata.
- Confirm `uv.lock` is unchanged.

### Per extraction commit

- Run focused owner-domain tests.
- Run facade compatibility tests.
- Run the architecture/layer guard.
- Run Ruff and mypy on touched production/test paths.
- Run `git diff --check` and inspect staged paths.

### Final local acceptance

- Run the canonical Python 3.12 compatibility lane.
- Run the canonical Python 3.13 coverage lane and require repository and controller/package non-regression.
- Run the five focused PostgreSQL testcontainer files and the complete testcontainer suite once.
- Run every locally owned source-sensitive custom gate that does not depend on out-of-band release metadata.
- Build and install the wheel using publishable `webui,llm,aws,postgres` extras.
- Build and smoke the local container because the refactor changes packaged executable code.
- In both wheel and container smokes:
  - import `psycopg` and `psycopg2`;
  - assert SQLAlchemy selects `psycopg2` for `postgresql://` and `psycopg` for `postgresql+psycopg://`;
  - verify the image label `io.elspeth.install-extras` equals `webui llm aws postgres` where applicable;
  - import `elspeth.web.aws_ecs_acceptance`;
  - run `--help`; and
  - require the deterministic scenario probe output.

The exact commands and current expected counts belong in the implementation plan after the base is selected. Do not copy stale literals from the superseded plan.

## Out-of-band release handoff

After final local acceptance:

1. Freeze source and record `FROZEN_SOURCE_SHA` and tree identity.
2. Confirm the implementation worktree is clean and still names that commit.
3. Record the selected `BASE_SHA`, frozen source commit and tree, extraction commit range, and local verification results on the single Filigree parent.
4. Hand that immutable source identity and evidence to the release owner.

This plan contains no Judge, signature, HMAC, fingerprint-baseline, release-candidate CI, push, or landing commands. The out-of-band release process owns every such action and decides how the frozen source enters the release.

## Rollback

Before handoff, each extraction commit is independently revertible to the prior green milestone. After handoff, source rollback and any consequences for release metadata belong to the out-of-band release process.

The refactor does not mutate the live ECS service, so deployment rollback is outside this design.

## Alternatives considered

### Coarse 13-module split

This option reduces file and commit count but leaves several 800- to 1,200-line modules that mix receipt validation with HTTP/capture work or combine independent S3 and Bedrock lanes. It postpones the same maintainability problem and weakens dependency boundaries.

### Partial split before 0.7.2

This option extracts only foundations and defers the remaining domains. It leaves a mixed architecture, creates more future movement, and does not satisfy the reason for completing the refactor before 0.7.2.

### Sign before and after the refactor

This option performs release authorization before the structural work, moves hundreds of source-bound judgments, and repeats authorization afterward. It creates avoidable operator work and was explicitly rejected. All release authorization remains out of band after source handoff.

## Success criteria

- The executable/import facade remains compatible across all commands and dispatch leaves.
- Public symbols are re-exported by identity.
- Security, protected-I/O, telemetry, provider, receipt, approval, manifest, ledger, and cleanup invariants retain behavioral coverage.
- Private imports obey the documented layer direction and contain no cycles.
- No baseline test identity disappears and controller/package coverage does not regress from the selected base.
- Wheel and container smokes prove both PostgreSQL drivers, both URL dialects, runtime extras, CLI startup, and deterministic behavior.
- All local refactor gates pass against the frozen source commit.
- The handoff records the frozen source commit, tree identity, selected base, commit range, clean worktree, and verification evidence.
- Signing, generated release metadata, release CI, branch pushes, and landing remain outside this plan.
- Live ECS deployment remains separate work.

## Superseded planning artifacts

After the developer approves this design, replace `docs/superpowers/plans/2026-07-22-aws-ecs-acceptance-refactor.md` with a new plan derived from this specification. Delete its stale `.review.json`; do not generate another review sidecar.
