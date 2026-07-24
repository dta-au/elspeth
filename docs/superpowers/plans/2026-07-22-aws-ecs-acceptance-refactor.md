# AWS ECS Acceptance Controller Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `src/elspeth/web/aws_ecs_acceptance.py` into focused private modules during 0.7.2 while preserving its executable/import facade, command-line behavior, security invariants, runtime approval verification, receipt/manifest semantics, and operational outputs.

**Architecture:** Keep `elspeth.web.aws_ecs_acceptance` as the permanent facade containing `build_parser()`, `main()`, output helpers, compatibility re-exports, and the `__main__` guard. Extract implementation into `elspeth.web._aws_ecs_acceptance` in the dependency order defined by the approved design. Move each domain's tests with its production owner, keep every commit green, and enforce layers plus the load-bearing forbidden edges.

**Tech Stack:** Python 3.12/3.13, argparse, httpx, boto3/botocore, SQLAlchemy, psycopg/psycopg2, pytest/pytest-cov, Ruff, mypy, uv, Docker, and Filigree.

---

## Read this first

The approved design is `docs/superpowers/specs/2026-07-24-aws-ecs-acceptance-refactor-design.md`. It is authoritative when this plan and the implementation appear to disagree.

This is a source-only refactor plan. It ends after a clean, locally verified source commit and immutable SHA/tree handoff. Release authorization, generated release metadata, remote release checks, branch publication, landing, and live ECS deployment are outside this plan and happen out of band. Do not add those activities to a task, verification gate, rollback step, or completion checklist.

Runtime approval-signature verification is application behavior and remains in scope. In particular, `approvals.py` must retain the existing Ed25519/keyring verification, closed approval schema, authority/run/plan/receipt/scenario bindings, expiry handling, injected-verifier support, and static non-leaking failures.

## Fixed scope

Production paths:

- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/__init__.py`
- Create modules under: `src/elspeth/web/_aws_ecs_acceptance/`

Test paths:

- Modify and progressively empty: `tests/unit/web/test_aws_ecs_acceptance.py`
- Create domain tests under: `tests/unit/web/aws_ecs_acceptance/`
- Create: `tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py`
- Modify only when adding compatibility assertions: `tests/unit/web/test_aws_ecs_runbook_contract.py`
- Modify only for reviewed owner-path movement: `tests/unit/web/test_landscape_access_guard.py`

Protected paths and behavior:

- Do not edit `docs/runbooks/aws-ecs-deployment.md`.
- Do not edit `uv.lock`, dependency declarations, deployment configuration, or unrelated source/tests.
- Do not rename commands/options, change argument conversion, alter output schemas, relax closed-field validation, change exception/error envelopes, change retry or pagination bounds, or change cleanup order.
- Do not convert `aws_ecs_acceptance.py` into a same-named package.
- Do not create a framework for other controllers.
- Use one Filigree parent task for the refactor; do not create one child issue per extraction commit.
- Do not create a review sidecar, receipt, manifest, or approval chain for this plan.

The tracked runbook must remain byte-identical to the selected base. Its invocation count and the characterized parser/dispatcher are compatibility evidence only; this plan does not certify or execute the exhaustive AWS runbook.

## Target package and dependency direction

```text
src/elspeth/web/_aws_ecs_acceptance/
├── __init__.py
├── contracts.py
├── secure_documents.py
├── state.py
├── http_client.py
├── capture.py
├── receipt_contracts.py
├── s3.py
├── bedrock.py
├── operator_telemetry.py
├── manifest_schema.py
├── scenario_inventory.py
├── manifest.py
├── task_definition.py
├── orphan_sweep.py
├── receipt_store.py
├── approvals.py
├── evidence.py
├── gate_ledger.py
├── cleanup.py
└── control_service.py
```

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

Imports may point only to the same or a lower-numbered layer. Also enforce:

- no private module imports the facade;
- `s3.py`, `bedrock.py`, and `operator_telemetry.py` do not import one another;
- `manifest_schema.py` and `scenario_inventory.py` do not import mutation services;
- `receipt_store.py` does not import `manifest.py` or `control_service.py`;
- `gate_ledger.py` does not import `evidence.py`, `cleanup.py`, or `control_service.py`;
- `cleanup.py` and `control_service.py` do not import one another; and
- `__init__.py` has no imports or other side effects.

## Execution and commit discipline

Use `BASE_SHA` as an explicit developer-selected full commit ID. Never derive it with `merge-base`, never silently advance it, and never begin from a red base. The selected commit must contain this plan and the approved design. The developer must declare `release/0.7.2` frozen for this refactor before Task 0.

Recheck the remote branch in every common extraction gate, every milestone, before the Task 13 review, and at final handoff. Any drift is a hard stop: do not merge, rebase, or redefine `BASE_SHA` in the active worktree. Return to the release owner for a newly selected exact base, then restart from a clean attempt-specific worktree/branch, recapture all dynamic baselines, replay the extraction series, and rerun every gate. Preserve and reuse the same Filigree `PARENT_ID` across attempts; record the abandoned base/attempt before restarting.

Use this shell setup in every new executor shell:

```bash
set -Eeuo pipefail
: "${BASE_SHA:?export the developer-selected release/0.7.2 commit}"
: "${PARENT_ID:?export the single Filigree parent issue ID}"
WORKTREE="/home/john/elspeth-aws-ecs-acceptance-${BASE_SHA:0:12}"
IMPLEMENTATION_BRANCH="refactor/aws-ecs-acceptance-${BASE_SHA:0:12}"
cd "$WORKTREE"
test "$(git rev-parse "${BASE_SHA}^{commit}")" = "$BASE_SHA"
```

For every extraction task:

1. Move complete symbols with history-preserving edits; do not copy a second implementation.
2. Import the new owner into the facade and explicitly re-export every existing public symbol by identity.
3. Move the owning tests in the same commit. Preserve test function/class names, parametrization IDs, decorators, setup, calls, and assertions. Only imports, owner paths, and monkeypatch targets should change for a mechanical move.
4. Add a focused regression before changing behavior if the move exposes a real defect. Do not hide a scoped defect as an observation.
5. Run the task's focused tests and the common extraction gate.
6. Stage only the paths listed by the task, inspect `git diff --cached --stat` and `git diff --cached --check`, then commit with the stated message.
7. Never start the next task on a red commit.

Common extraction gate, used after Tasks 2-12:

```bash
set -Eeuo pipefail
test "$(git ls-remote origin refs/heads/release/0.7.2 | awk '{print $1}')" = "$BASE_SHA"
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/web/test_landscape_access_guard.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/architecture/test_sink_publication_callers.py -q
env -u VIRTUAL_ENV uv run --frozen ruff check \
  src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py
env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py
env -u VIRTUAL_ENV uv run --frozen mypy \
  src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance
git diff --check
git diff --exit-code "$BASE_SHA" -- uv.lock docs/runbooks/aws-ecs-deployment.md
```

At the milestone ends named below, additionally run:

```bash
set -Eeuo pipefail
BASELINE_DIR=.elspeth/aws-ecs-acceptance-refactor
test "$(git ls-remote origin refs/heads/release/0.7.2 | awk '{print $1}')" = "$BASE_SHA"
COVERAGE_FILE="$BASELINE_DIR/current.coverage" \
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_runbook_contract.py -q \
  --cov=elspeth.web.aws_ecs_acceptance \
  --cov=elspeth.web._aws_ecs_acceptance \
  --cov-branch --cov-report="json:$BASELINE_DIR/current-controller-coverage.json"
.venv/bin/python - \
  "$BASELINE_DIR/base-controller-coverage-fraction.txt" \
  "$BASELINE_DIR/current-controller-coverage.json" <<'PY'
import json
import sys

baseline_covered, baseline_total = map(int, open(sys.argv[1], encoding="utf-8").read().split())
with open(sys.argv[2], encoding="utf-8") as stream:
    totals = json.load(stream)["totals"]
current_covered = totals["covered_lines"] + totals["covered_branches"]
current_total = totals["num_statements"] + totals["num_branches"]
if current_covered * baseline_total < baseline_covered * current_total:
    raise SystemExit(
        f"controller coverage regressed: {current_covered}/{current_total} "
        f"< {baseline_covered}/{baseline_total}"
    )
PY
env -u VIRTUAL_ENV uv run --frozen pytest --collect-only -q \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  | sed -n 's/^[^:]*:://p' | LC_ALL=C sort \
  > "$BASELINE_DIR/current-normalized-node-ids.txt"
comm -23 \
  "$BASELINE_DIR/base-normalized-node-ids.txt" \
  "$BASELINE_DIR/current-normalized-node-ids.txt" \
  > "$BASELINE_DIR/missing-normalized-node-ids.txt"
test ! -s "$BASELINE_DIR/missing-normalized-node-ids.txt"
```

This permits new regression tests and test-file movement but rejects disappearance or renaming of a baseline test identity. A lower controller/package coverage percentage blocks the milestone even if the repository-wide floor still passes.

---

### Task 0: Select and qualify the green base

**Files:**

- Read: `/home/john/elspeth/AGENTS.md` (workspace instructions are host-local and not tracked in the selected base)
- Read: `docs/superpowers/specs/2026-07-24-aws-ecs-acceptance-refactor-design.md`
- Read: `src/elspeth/web/aws_ecs_acceptance.py`
- Read: `tests/unit/web/test_aws_ecs_acceptance.py`
- Create ignored evidence only under: `.elspeth/aws-ecs-acceptance-refactor/`

- [ ] **Step 1: Fail closed on the selected remote base and create the worktree**

From `/home/john/elspeth`:

```bash
set -Eeuo pipefail
: "${BASE_SHA:?developer must export the reviewed full release base SHA}"
TARGET_RELEASE_BRANCH=release/0.7.2
IMPLEMENTATION_BRANCH="refactor/aws-ecs-acceptance-${BASE_SHA:0:12}"
WORKTREE="/home/john/elspeth-aws-ecs-acceptance-${BASE_SHA:0:12}"
remote_sha=$(git ls-remote origin "refs/heads/$TARGET_RELEASE_BRANCH" | awk '{print $1}')
test -n "$remote_sha"
test "$remote_sha" = "$BASE_SHA"
git fetch --no-tags origin "refs/heads/$TARGET_RELEASE_BRANCH"
test "$(git rev-parse FETCH_HEAD^{commit})" = "$BASE_SHA"
test "$(git ls-remote origin "refs/heads/$TARGET_RELEASE_BRANCH" | awk '{print $1}')" = "$BASE_SHA"
test "$(git cat-file -t "$BASE_SHA")" = commit
test ! -e "$WORKTREE"
! git show-ref --verify --quiet "refs/heads/$IMPLEMENTATION_BRANCH"
git worktree add "$WORKTREE" -b "$IMPLEMENTATION_BRANCH" "$BASE_SHA"
cd "$WORKTREE"
test "$(git rev-parse HEAD)" = "$BASE_SHA"
test -z "$(git status --porcelain)"
test -f docs/superpowers/plans/2026-07-22-aws-ecs-acceptance-refactor.md
test -f docs/superpowers/specs/2026-07-24-aws-ecs-acceptance-refactor-design.md
test -f /home/john/elspeth/AGENTS.md
env -u VIRTUAL_ENV uv sync --frozen --all-extras
git diff --exit-code -- uv.lock
```

Expected: the remote branch still names the developer-selected commit, the isolated worktree starts exactly there, the plan/design are present, dependencies sync without lock-file movement, and the worktree is clean. If the remote moved, stop and ask the developer to select a new base; do not merge or repin autonomously.

- [ ] **Step 2: Reuse or create and atomically start one Filigree parent**

First use Filigree MCP `issue_search` for the exact title and `release-0.7.2`/`aws-ecs` labels; CLI fallback:

```bash
filigree search \
  "Refactor AWS ECS acceptance controller into private modules" \
  --json
```

Inspect every match and ignore terminal issues. If one exact-scope non-terminal parent already exists, reuse it: resume an in-progress issue already held by `codex-aws-ecs` without calling `start-work`; use guarded `reclaim` only for a verified stale different holder; or atomically `start-work` an open ready issue. Do not create a duplicate and do not attach this work to the unrelated stale Plan 12 closeout.

If no exact-scope open parent exists, prefer Filigree MCP `issue_create` followed by `work_start`; CLI fallback:

```bash
filigree create \
  "Refactor AWS ECS acceptance controller into private modules" \
  --type task --priority 1 --assignee codex-aws-ecs \
  --label release-0.7.2 --label aws-ecs --label refactor \
  --description "Execute docs/superpowers/plans/2026-07-22-aws-ecs-acceptance-refactor.md as a source-only refactor; completion is frozen-source handoff." \
  --actor codex-aws-ecs --json
```

Copy the returned ID into `PARENT_ID`, then:

```bash
: "${PARENT_ID:?copy the created issue ID}"
filigree start-work "$PARENT_ID" \
  --assignee codex-aws-ecs --actor codex-aws-ecs \
  --commit "$IMPLEMENTATION_BRANCH@$BASE_SHA"
BASE_TREE=$(git rev-parse "${BASE_SHA}^{tree}")
filigree add-comment "$PARENT_ID" \
  "Selected source base: BASE_SHA=$BASE_SHA BASE_TREE=$BASE_TREE" \
  --actor codex-aws-ecs
```

Expected: one parent is in progress. Do not create child issues for the numbered tasks.

- [ ] **Step 3: Capture dynamic compatibility and coverage evidence**

```bash
set -Eeuo pipefail
BASELINE_DIR=.elspeth/aws-ecs-acceptance-refactor
install -d -m 0700 "$BASELINE_DIR"
printf '%s\n' "$BASE_SHA" > "$BASELINE_DIR/base-sha.txt"
git rev-parse "${BASE_SHA}^{tree}" > "$BASELINE_DIR/base-tree.txt"
env -u VIRTUAL_ENV uv run --frozen pytest --collect-only -q \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  | sed -n 's/^[^:]*:://p' | LC_ALL=C sort \
  > "$BASELINE_DIR/base-normalized-node-ids.txt"
COVERAGE_FILE="$BASELINE_DIR/base.coverage" \
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/test_aws_ecs_runbook_contract.py -q \
  --cov=elspeth.web.aws_ecs_acceptance --cov-branch \
  --cov-report="json:$BASELINE_DIR/base-controller-coverage.json"
.venv/bin/python - "$BASELINE_DIR/base-controller-coverage.json" \
  > "$BASELINE_DIR/base-controller-coverage-fraction.txt" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    totals = json.load(stream)["totals"]
covered = totals["covered_lines"] + totals["covered_branches"]
total = totals["num_statements"] + totals["num_branches"]
print(covered, total)
PY
test -s "$BASELINE_DIR/base-normalized-node-ids.txt"
test -s "$BASELINE_DIR/base-controller-coverage-fraction.txt"
env -u VIRTUAL_ENV uv run --frozen python -m elspeth.web.aws_ecs_acceptance \
  scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A \
  | tee "$BASELINE_DIR/scenario-namespace.txt"
test "$(<"$BASELINE_DIR/scenario-namespace.txt")" = a-f8b447a7b5b51f38c800
```

Expected: current node identities and exact branch-inclusive controller coverage are recorded from `BASE_SHA`; the deterministic probe prints `a-f8b447a7b5b51f38c800`. Do not paste historical test counts or percentages into the implementation.

- [ ] **Step 4: Qualify the base with locally owned source gates**

Run, fail-fast:

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen ruff check src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen ruff format --check src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen mypy src/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/check_slot_type_cross_language.py
env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/generate_skill_inventory.py --check
env -u VIRTUAL_ENV uv run --frozen python scripts/check_contracts.py
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules plugin_contract.options_metadata --root .
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules plugin_contract.component_type,plugin_contract.plugin_hashes --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules immutability.freeze_guards,immutability.frozen_annotations --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules audit_evidence.nominal_base,audit_evidence.tier_1_decoration,audit_evidence.guard_symmetry,audit_evidence.gve_attribution --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules 'composer/*' --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules 'contract_invariants/*' --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules contract_invariants.session_engine_factory --root .
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules manifest.contract_manifest --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules manifest.symbol_inventory,manifest.test_to_source_mapping --root .
# Key-free, non-mutating source honesty lint. The configured honesty allowlist budget is zero;
# this neither creates nor updates release authorization metadata.
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python -m elspeth_lints.core.cli check --rules meta.no-new-bespoke-cicd-enforcer --root .
env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/enforce_adapter_budget.py
PYTHONPATH=elspeth-lints/src env -u VIRTUAL_ENV uv run --frozen python scripts/cicd/parity_harness.py --manifest config/cicd/lint_migration_status.yaml --root .
git diff --exit-code -- uv.lock docs/runbooks/aws-ecs-deployment.md
```

Expected: every command exits zero. A pre-existing failure blocks the refactor and must be repaired as separately scoped work on the release branch before a new `BASE_SHA` is selected.

- [ ] **Step 5: Run the full local base lanes**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --isolated --python 3.12 --frozen --all-extras pytest tests/ \
  -v -m "not slow and not stress and not performance and not testcontainer and not live_aws and not fingerprint_baseline"
env -u VIRTUAL_ENV uv run --frozen pytest tests/ \
  --cov=src/elspeth --cov-report=xml --cov-report=term-missing --cov-fail-under=85 \
  -v -m "not slow and not stress and not performance and not testcontainer and not live_aws and not fingerprint_baseline"
coverage_status=0
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/core/landscape/*' --fail-under=92 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/core/canonical.py' --fail-under=99 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/engine/orchestrator/*' --fail-under=90 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/contracts/*' --fail-under=62 || coverage_status=1
test "$coverage_status" -eq 0
env -u VIRTUAL_ENV uv run --frozen pytest tests/testcontainer/ -v -m testcontainer
```

Expected: both Python lanes, all four aggregate coverage checks, and the complete PostgreSQL testcontainer suite pass. Docker/testcontainer unavailability is an unsatisfied prerequisite, not a skip.

Before the base packaging smokes, prepare the immutable candidate inputs consumed by Task 14 Step 6:

```bash
git rev-parse HEAD^{commit} > .elspeth/aws-ecs-acceptance-refactor/candidate-sha.txt
git rev-parse HEAD^{tree} > .elspeth/aws-ecs-acceptance-refactor/candidate-tree.txt
```

Build and smoke the base wheel and local image using the exact commands in Task 14 Steps 5 and 6. At the untouched base, `HEAD` is `BASE_SHA`; both smokes must pass before extraction begins. Task 14 overwrites these candidate inputs after the refactor and cleanup review.

**Definition of Done:** developer-declared frozen branch and exact green base selected, isolated worktree created, one parent issue started, dynamic evidence captured, and all source-only local base gates green. No commit.

---

### Task 1: Characterize the permanent facade and install the layer guard

**Files:**

- Create: `tests/unit/web/aws_ecs_acceptance/__init__.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_facade_contract.py`
- Create: `tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py`
- Modify only if needed for new assertions: `tests/unit/web/test_aws_ecs_runbook_contract.py`

- [ ] **Step 1: Add explicit facade tables**

In `test_facade_contract.py`, add reviewed constants rather than generated snapshots:

- `EXPECTED_COMMANDS`: all top-level commands from the selected base;
- `EXPECTED_DISPATCH_TARGETS`: every leaf command path mapped to the called public function and argument conversion contract;
- `EXPECTED_PUBLIC_EXPORTS`: every module-defined non-private function/class/assignment on the selected base; and
- expected `scenario-namespace` stdout plus representative safe stderr/error envelopes.

Implement a recursive `_parser_surface()` that records command path, action class, destination, sorted option strings, `required`, `nargs`, sorted choices, stable callable identity for `type`, `default`, `const`, `metavar`, and mutually exclusive group membership. Do not infer required groups from individual actions.

Add tests that prove:

- parser surface and all nested dispatch leaves match the explicit tables;
- `main()` preserves return codes, stdout/stderr separation, JSON shape, and static unexpected-error output;
- `python -m elspeth.web.aws_ecs_acceptance --help` remains executable;
- every expected public export is directly importable from the facade; and
- `scenario_resource_namespace()` and the CLI probe retain the selected-base value.

- [ ] **Step 2: Add the architecture test before the package exists**

In `test_aws_ecs_acceptance_dependencies.py`, encode the layer map and forbidden edges from this plan. Parse `ast.Import` and `ast.ImportFrom`; resolve relative imports under `elspeth.web._aws_ecs_acceptance`. Until the private directory exists, the test should assert only that the facade does not already import an unexpected private implementation. Once modules appear, enforce every discovered private-to-private edge and fail on an unlisted module.

Use a layer map, not an exact whole-graph snapshot:

```python
LAYERS = {
    "contracts": 0,
    "secure_documents": 1, "state": 1, "http_client": 1, "receipt_contracts": 1,
    "capture": 2, "s3": 2, "bedrock": 2, "operator_telemetry": 2,
    "manifest_schema": 2, "scenario_inventory": 2, "gate_ledger": 2,
    "manifest": 3, "task_definition": 3, "orphan_sweep": 3,
    "receipt_store": 3, "approvals": 3, "evidence": 3,
    "cleanup": 4, "control_service": 4,
}
```

Also assert `__init__.py` is empty except for a docstring/comments. Build the discovered private-module import graph and fail on every cycle, including cycles that otherwise obey numeric layer ordering.

- [ ] **Step 3: Run and commit the characterization**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py -q
git diff --check
git status --short
git add \
  tests/unit/web/aws_ecs_acceptance/__init__.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/web/test_aws_ecs_runbook_contract.py
git diff --cached --check
git commit -m "test(web): characterize AWS ECS acceptance facade"
```

Expected: tests pass against the untouched monolith and no production file changes in this commit.

**Definition of Done:** explicit facade/dispatch/export contract and permanent architecture guard committed before production movement.

---

### Task 2: Extract contracts, protected documents, and state

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/__init__.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/contracts.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/secure_documents.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/state.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_contracts_secure_state.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`
- Modify: `tests/unit/web/aws_ecs_acceptance/test_facade_contract.py`

- [ ] **Step 1: Move foundational contracts**

Move to `contracts.py` without behavioral edits:

- acceptance error classes;
- closed field sets, bounds, command-independent constants, identity/hash/UUID/time helpers;
- a domain-neutral strict UTC `Z` timestamp parser that raises `ValueError`, plus the Layer-0 `_control_timestamp` wrapper retaining its current `AcceptanceCheckError` mapping; `state.py` keeps the state-specific wrapper and `AcceptanceStateError` mapping;
- `_bounded_identity` and `SanitizedResourceIdentity`, so receipt validation and telemetry share one downward owner; and
- a pure ECS task-definition-family parser that returns a family or no match, with scenario/orphan wrappers preserving their different existing error identifiers;
- `normalize_acceptance_origin`, `scenario_resource_namespace`, and `plugin_policy_binding_sha256`;
- shared mapping/string/UUID/SHA extraction helpers; and
- `_resolve_aws_region`, keeping the caller-supplied `check` identifier and fail-closed region rules.

Do not place AWS clients or domain orchestration in `contracts.py`.

- [ ] **Step 2: Move the complete protected-document transaction**

Move parent/destination/stat validation, protected document reads/writes, `_open_receipt_manifest_lock`, `_receipt_manifest_write_lock`, and `_serialized_control_manifest_write` to `secure_documents.py`.

Preserve, with focused tests:

- owner and exact mode `0600` checks;
- no-follow opens and inode/device validation;
- hard-link publication for a new lock;
- `flock` covering the complete read-modify-write transaction;
- temporary write, file `fsync`, atomic replace, and parent-directory `fsync`;
- cleanup of failed temporary/lock publications; and
- static errors that do not reveal path content or secrets.

Add a cross-process lost-update regression that pauses the first writer after reading, starts a second writer, and proves the second cannot enter its read-modify-write section until the first publishes and unlocks. A lock held only around the final write must fail this test.

- [ ] **Step 3: Move state ownership and tests**

Move `AcceptanceCredentials`, `AcceptanceState`, state parsing/serialization/timestamp wrappers, and `read_acceptance_state`/`write_acceptance_state` to `state.py`. The state wrapper consumes the domain-neutral UTC parser and maps failures to the unchanged state-schema error. Keep protected-file behavior identical.

Move the owning test functions into `test_contracts_secure_state.py`; preserve normalized node identities and retarget monkeypatches to the defining module.

- [ ] **Step 4: Re-export, verify, and commit**

The facade must import and explicitly re-export every moved public symbol by identity. `__init__.py` remains side-effect-free.

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/aws_ecs_acceptance/test_contracts_secure_state.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py -q
# Run the common extraction gate, then the milestone gate.
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_contracts_secure_state.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py
git diff --cached --check
git commit -m "refactor(web): extract acceptance foundations"
```

**Definition of Done:** foundations have single owners; serialized mutation remains cross-process safe; facade identity, node identity, and dynamic coverage gates pass.

---

### Task 3: Extract receipt contracts, HTTP transport, and capture

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/http_client.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/capture.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_http_capture.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_landscape_access_guard.py`

- [ ] **Step 1: Consolidate pure receipt validation**

Move exec-receipt schema validation, environment resolution, encoding/extraction, and all pure S3/Bedrock/guardrail/operator/Terraform/event-canary/compatibility-receipt/bounded/stored-receipt validators to `receipt_contracts.py`. Move the operator receipt-schema constants used by both validation and telemetry into this module. Operator receipt validation imports `SanitizedResourceIdentity` from `contracts.py`; it must not depend on `operator_telemetry.py`. `operator_telemetry.py` later imports/re-exports these downward-owned constants where compatibility requires. Keep the public `validate_compatibility_record` operation out of this layer because it reads and composes control-manifest state; Task 12 moves it to `control_service.py`. Move provider-neutral receipt constants to `contracts.py` when needed to avoid an upward import.

Preserve exact closed keys, type rejection (including `bool` as a number), lengths, hashes, namespaces, environment conflict handling, base64 encoding, and static error identifiers. Keep this module free of filesystem writes and AWS clients.

- [ ] **Step 2: Move bounded HTTP ownership**

Move `AcceptanceHttpClient` and request/authentication/response helpers to `http_client.py`. Preserve timeout/retry budgets, authorization construction, bounded response reads, content-type rules, redirect behavior, status handling, and safe errors.

- [ ] **Step 3: Move capture and local verification**

Move fixed/tutorial pipeline builders, run/artifact selection, `capture`, plugin-policy HTTP verification, `verify_api`, `verify_local_auth`, `provision_storage`, and `verify_payloads` to `capture.py`. Preserve exact request order, run facts, artifact selection, payload hashes, and receipt construction.

Move owning tests into the two new test modules. Retarget patches to `http_client`, `capture`, or `receipt_contracts`; do not retain facade monkeypatches that no longer affect the defining module.

- [ ] **Step 4: Verify and commit**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
  tests/unit/web/aws_ecs_acceptance/test_http_capture.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/web/test_landscape_access_guard.py -q
# Run the common extraction gate.
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/contracts.py \
  src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py \
  src/elspeth/web/_aws_ecs_acceptance/http_client.py \
  src/elspeth/web/_aws_ecs_acceptance/capture.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
  tests/unit/web/aws_ecs_acceptance/test_http_capture.py \
  tests/unit/web/test_landscape_access_guard.py
git diff --cached --check
git commit -m "refactor(web): extract acceptance transport and receipts"
```

**Definition of Done:** pure receipt contracts, bounded HTTP transport, and capture/local checks have distinct owners with unchanged behavior.

---

### Task 4: Extract the S3 acceptance lane

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/s3.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_s3.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move `_S3AcceptanceContext`, S3 input resolution, not-found classification, source hashing, effect identity, `_drive_s3_acceptance_effect`, `verify_s3`, and S3-only constants to `s3.py`.

- [ ] Import the single `_resolve_aws_region` owner from `contracts.py`. Do not import Bedrock or telemetry modules.

- [ ] Move S3 tests to `test_s3.py` and preserve publication-before-cleanup, retry bounds, conditional put/delete behavior, receipt hashes, error classification, and non-leaking failures.

- [ ] Verify and commit:

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/aws_ecs_acceptance/test_s3.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py -q
# Run the common extraction gate.
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/s3.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_s3.py
git diff --cached --check
git commit -m "refactor(web): extract S3 acceptance lane"
```

**Definition of Done:** S3 has one owner and no cross-domain dependency.

---

### Task 5: Extract Bedrock, guardrails, and plugin-policy acceptance

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/bedrock.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_landscape_access_guard.py`

- [ ] Move process-output suppression, Bedrock content/receipt projection, `verify_bedrock`, operator-profile construction, guardrail input validation, `_AcceptanceSecretInventory`, `build_plugin_policy_acceptance`, `verify_bedrock_guardrails`, telemetry-manager construction, and `run_bedrock_guardrails_live` to `bedrock.py`.

- [ ] Preserve provider/model/region admission, secret redaction, plugin-policy binding, exact record selection, guardrail denial behavior, output suppression restoration, and cost/request identity hashes.

- [ ] Import `_resolve_aws_region` from `contracts.py`; do not import S3 or operator telemetry.

- [ ] Move and retarget owning tests, then verify and commit:

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/web/test_landscape_access_guard.py -q
# Run the common extraction gate.
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/bedrock.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py \
  tests/unit/web/test_landscape_access_guard.py
git diff --cached --check
git commit -m "refactor(web): extract Bedrock acceptance lane"
```

**Definition of Done:** Bedrock/guardrail/plugin-policy acceptance is isolated without weakening secret or provider checks.

---

### Task 6: Extract operator telemetry and connection-budget verification

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/operator_telemetry.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_operator_telemetry.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_landscape_access_guard.py`

- [ ] Move telemetry protocols, policy/evidence types, metric dimensions, AWS query/emitter adapters, lifecycle adapters, forbidden-content checks, telemetry/outage/live verification, PostgreSQL max-connection reads, and `verify_connection_budget_live` to `operator_telemetry.py`. Import the shared sanitized resource identity from `contracts.py`; do not define a second copy.

- [ ] Preserve durable trace identity. `xray_trace_id(run_id, *, started_at)` must use the persisted Landscape run-start time. Both public and existing-run lifecycle adapters must load/cache `_read_landscape_started_at`; every X-Ray query and retained trace identifier must receive that same value. Never substitute current wall-clock time.

- [ ] Add or retain regressions for:

- identical trace IDs across retries at different wall-clock times;
- public and existing-run adapters using the durable start time;
- fail-closed missing/invalid start time;
- CloudWatch/X-Ray pagination and query bounds;
- outage evidence versus successful evidence;
- forbidden-value absence; and
- connection-budget arithmetic and closed receipt shape.

- [ ] Import `_resolve_aws_region` from `contracts.py`; do not import S3 or Bedrock. Move/retarget owner tests, run the common gate and milestone gate, then commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/operator_telemetry.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_operator_telemetry.py \
  tests/unit/web/test_landscape_access_guard.py
git diff --cached --check
git commit -m "refactor(web): extract operator telemetry acceptance"
```

**Definition of Done:** telemetry and connection-budget ownership is isolated; durable start-time identity, test identity, and dynamic coverage are preserved.

---

### Task 7: Extract control-manifest and scenario-inventory schemas

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/manifest_schema.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/scenario_inventory.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_manifest_schema_inventory.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move control-manifest validation/read helpers, closed field/order constants, retained-evidence validation, and `_require_mutable_control_manifest` to `manifest_schema.py`.

- [ ] Move `SCENARIO_ASSIGNMENT_NAMES`, scenario inventory hash/schema validation, Terraform binding validation, listener resolution, resource binding/resolved-value validation, isolation, and pre-apply/bound inventory loads to `scenario_inventory.py`. Consume the pure ECS task-family parser from `contracts.py` and map invalid values to the existing scenario-inventory binding failure.

- [ ] Keep both modules validation-only. They may use `contracts`, `secure_documents`, and `receipt_contracts`; they must not import mutation, receipt-store, approval, evidence, cleanup, or control-service modules.

- [ ] Add/retain a table-driven finalization test that invokes every manifest mutator after `final_evidence.phase == "committed"` and requires the existing `control_manifest_finalized` failure. Consumers must call the guard through its owner module rather than copy a second implementation. Post-commit cleanup replay is the sole validation-only exception and must make no write.

- [ ] Move/retarget owner tests, run the common gate, and commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/manifest_schema.py \
  src/elspeth/web/_aws_ecs_acceptance/scenario_inventory.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_manifest_schema_inventory.py
git diff --cached --check
git commit -m "refactor(web): extract acceptance manifest schemas"
```

**Definition of Done:** validation ownership is isolated and immutable finalization remains universal and fail-closed.

---

### Task 8: Extract manifest mutations and task-definition admission

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/manifest.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/task_definition.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_manifest_task_definition.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move low-level serialized manifest initialization, retained-evidence binding, operator checkpointing, scenario binding, and field/read operations to `manifest.py`. Every mutation must retain `_serialized_control_manifest_write` or explicitly hold the same lock for the full read-modify-write transaction, and must call `_require_mutable_control_manifest` before changing state.

- [ ] Move plaintext-secret classification, Secrets Manager ARN/inventory binding, and `validate_task_definition_policy_binding` to `task_definition.py`.

- [ ] Preserve provider-aware admission for both `ELSPETH_WEB__COMPOSER_MODEL` and `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL`: infer both providers, require `OPENROUTER_API_KEY` exactly when either provider is OpenRouter, and reject it otherwise. Preserve image, task/execution roles, AWS override rejection, environment closure, EFS/runtime paths, secret selectors, inventory IDs, and plugin-policy bindings.

- [ ] Add a provider matrix covering Bedrock/Bedrock, OpenRouter/Bedrock, Bedrock/OpenRouter, OpenRouter/OpenRouter, unknown provider, missing required secret, and forbidden surplus OpenRouter secret.

- [ ] Move/retarget owner tests, run the common gate, and commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/manifest.py \
  src/elspeth/web/_aws_ecs_acceptance/task_definition.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_manifest_task_definition.py
git diff --cached --check
git commit -m "refactor(web): extract manifest and task policy"
```

**Definition of Done:** low-level mutations remain serialized/finalization-aware and task-definition admission remains provider-correct.

---

### Task 9: Extract orphan lifecycle ownership

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/orphan_sweep.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_orphan_sweep.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move `OrphanSweepClients`, client construction, AWS error classification, bounded calls/pagination, inventory projection, task-family ownership, transaction-search projection, and `orphan_sweep` to `orphan_sweep.py`. Its task-family wrapper consumes the pure parser from `contracts.py` and preserves the existing orphan API/binding error mapping.

- [ ] Preserve page/item/retry budgets, accepted not-found errors, namespace/ownership proof, orphan/survivor counts, dry-run versus destructive behavior, and failure-safe receipt output.

- [ ] Move/retarget owner tests, run the common gate, and commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/orphan_sweep.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_orphan_sweep.py
git diff --cached --check
git commit -m "refactor(web): extract orphan sweep ownership"
```

**Definition of Done:** orphan discovery/cleanup is isolated with unchanged bounds and ownership proof.

---

### Task 10: Extract receipt persistence and runtime approvals

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/receipt_store.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/approvals.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_receipts_approvals.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move bounded receipt persistence, `receipt_store`, and `_receipt_store_locked` to `receipt_store.py`. Keep one receipt-manifest lock held across both receipt publication and manifest indexing. The module may consume pure validators but must not import `manifest.py` or `control_service.py`.

- [ ] Add/retain a cross-process regression that interleaves two distinct receipt publications and proves neither a lost index entry nor an unindexed published receipt is observable.

- [ ] Move approval base64url decoding, configured public-key verifier construction, `approval_verify`, `_require_current_approval`, `approval_require_current`, and serialized approval mutation to `approvals.py`.

- [ ] Preserve exact runtime keyring protection, Ed25519 verification, injected verifier behavior, closed sanitized content, logical/content identities, expiry, and plan/receipt/run/scenario/authority bindings. Never log key material or untrusted approval content.

- [ ] Move/retarget owner tests, run the common gate and milestone gate, then commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/receipt_store.py \
  src/elspeth/web/_aws_ecs_acceptance/approvals.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_receipts_approvals.py
git diff --cached --check
git commit -m "refactor(web): extract receipts and runtime approvals"
```

**Definition of Done:** receipt publication/indexing remains one atomic authority and runtime approval verification is unchanged.

---

### Task 11: Extract evidence and the gate ledger

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/evidence.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/gate_ledger.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_evidence_gate_ledger.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`

- [ ] Move safe value/log projection, evidence sanitization, evidence-export receipt validation/reverification/construction, stored-receipt verification, and the pure final-cleanup receipt document/verification helpers to `evidence.py`. Keeping the pure final-receipt verifier here lets control-manifest validation consume it without importing cleanup orchestration.

- [ ] Move gate record hashing/stream validation, ledger schema/read/init/get, candidate binding, ordered record append, cleanup record append, replay checks, and finalization to `gate_ledger.py`.

- [ ] Preserve redaction, closed projection, receipt aggregate identity, ordered gate prefixes, candidate binding, interruption/replay behavior, terminal record uniqueness, and serialized writes. `gate_ledger.py` must not import `evidence.py`, `cleanup.py`, or `control_service.py`; callers compose their results.

- [ ] Move/retarget owner tests, run the common gate and milestone gate, then commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/evidence.py \
  src/elspeth/web/_aws_ecs_acceptance/gate_ledger.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance/test_evidence_gate_ledger.py
git diff --cached --check
git commit -m "refactor(web): extract evidence and gate ledger"
```

**Definition of Done:** evidence and ordered gate-journal ownership are separate, closed, and replay-safe.

---

### Task 12: Extract cleanup/control orchestration and finish the facade

**Files:**

- Create: `src/elspeth/web/_aws_ecs_acceptance/cleanup.py`
- Create: `src/elspeth/web/_aws_ecs_acceptance/control_service.py`
- Create: `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py`
- Modify: `src/elspeth/web/aws_ecs_acceptance.py`
- Modify: `tests/unit/web/test_aws_ecs_acceptance.py`
- Modify: all owner tests under `tests/unit/web/aws_ecs_acceptance/` only if final fixture ownership requires it

- [ ] Move final cleanup receipt publication/ensure logic, two-phase cleanup preparation/commit, interruption recovery, idempotent post-commit verification, and `cleanup_evidence_finalize` to `cleanup.py`. Reuse the pure receipt document/verifier from `evidence.py`.

- [ ] Preserve the state machine exactly:

1. Prepare binds stored-receipt aggregates, ledger-prefix identity, and final evidence export without clearing `cleanup_required`.
2. Commit requires prepared evidence and verified cleanup surfaces, appends and verifies the terminal cleanup record, commits final hashes, and only then clears `cleanup_required`.
3. Replay after commit revalidates the terminal state without mutation.

Add failure-injection tests after each durable write and prove retry either completes safely or performs validation-only replay. Add a write spy proving committed replay performs zero writes.

- [ ] Move `control_manifest_validate`, `control_manifest_update`, `control_manifest_load_cleanup`, `scenario_load`, `validate_compatibility_record`, and high-level composition of manifest/receipt/approval/evidence/ledger services to `control_service.py`. Preserve function signatures and the existing single protected-document commit boundary. `control_service.py` and `cleanup.py` must not import one another.

- [ ] Reduce the facade to imports/re-exports, `build_parser()`, `main()`, `_print_json`, `_print_error`, `_write_stdout_line`, and the `__main__` guard. Remove every duplicate moved body. Do not use wildcard imports. Make the architecture test require the complete module set listed in this plan at closeout.

- [ ] Finish moving remaining owner tests out of `test_aws_ecs_acceptance.py`. It may remain as a small compatibility shim test, but must not retain domain tests merely to avoid retargeting patches. Run an AST/name inventory proving each selected-base normalized test identity still exists and each public symbol re-exports the private owner object by identity.

- [ ] Verify with the common extraction gate and milestone gate, then commit:

```bash
git add src/elspeth/web/aws_ecs_acceptance.py \
  src/elspeth/web/_aws_ecs_acceptance/cleanup.py \
  src/elspeth/web/_aws_ecs_acceptance/control_service.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance
git diff --cached --check
git commit -m "refactor(web): complete acceptance controller split"
```

**Definition of Done:** cleanup/control orchestration retains its two-phase authority and the permanent facade contains no domain implementation.

---

### Task 13: Run a first-principles subagent cleanup review

**Files:**

- Review: `src/elspeth/web/aws_ecs_acceptance.py`
- Review: every module under `src/elspeth/web/_aws_ecs_acceptance/`
- Review: `tests/unit/web/test_aws_ecs_acceptance.py`
- Review: every test under `tests/unit/web/aws_ecs_acceptance/`
- Review: `tests/unit/web/test_aws_ecs_runbook_contract.py`
- Review: `tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py`
- Modify only for validated findings: the fixed-scope production/test paths named at the start of this plan
- Record only: the single Filigree parent

This is a mandatory correctness cleanup, not a style pass and not a review artifact ceremony. Review agents are read-only. They inspect the complete smaller modules and direct callers/tests from first principles; they do not limit themselves to the extraction diff or trust earlier review conclusions.

- [ ] **Step 1: Establish the review candidate**

```bash
set -Eeuo pipefail
test -z "$(git status --porcelain)"
test "$(git ls-remote origin refs/heads/release/0.7.2 | awk '{print $1}')" = "$BASE_SHA"
REVIEW_SHA=$(git rev-parse HEAD^{commit})
REVIEW_TREE=$(git rev-parse HEAD^{tree})
printf '%s\n' "$REVIEW_SHA" > .elspeth/aws-ecs-acceptance-refactor/review-sha.txt
printf '%s\n' "$REVIEW_TREE" > .elspeth/aws-ecs-acceptance-refactor/review-tree.txt
git merge-base --is-ancestor "$BASE_SHA" "$REVIEW_SHA"
filigree add-comment "$PARENT_ID" \
  "Starting first-principles post-split review at REVIEW_SHA=$REVIEW_SHA REVIEW_TREE=$REVIEW_TREE" \
  --actor codex-aws-ecs
```

Expected: reviewers receive one clean, immutable candidate. Every first-wave reviewer must confirm `git rev-parse HEAD^{commit}` equals `REVIEW_SHA` before and after reviewing. No reviewer may edit the shared worktree. If the tree changes during the wave, discard the reports and restart from a new clean checkpoint.

- [ ] **Step 2: Dispatch seven independent read-only review lenses in parallel**

Use `superpowers:dispatching-parallel-agents`. Spawn every first-wave reviewer with `fork_turns="none"` and a self-contained prompt containing the approved design path, plan path, absolute `/home/john/elspeth/AGENTS.md` path, worktree, assigned files, and `REVIEW_SHA`; do not pass conversation or earlier-review history. Give every reviewer this common instruction:

```text
Review the complete assigned production modules, their facade call sites, and their
direct tests from first principles at REVIEW_SHA. Do not review only the diff and do
not assume the extraction preserved behavior. Trace data/control flow across module
boundaries, inspect failure and replay paths, and run narrow read-only tests or
reproductions when useful. Do not edit any file, update Filigree, contact live AWS,
run Docker/testcontainers, deploy, publish, or perform release work.
When running pytest, use `-p no:cacheprovider` and a reviewer-unique directory created
with `mktemp -d` as `--basetemp`; do not share pytest caches or temporary paths.

Report only concrete correctness, security, data-integrity, compatibility, bounded-resource,
or material test-gap findings. For each finding provide: lens-local ID; severity;
classification as refactor-introduced, pre-existing-exposed, or uncertain; file and line;
violated invariant; specific failure scenario; evidence/reproduction; user/system impact;
affected tests; the smallest safe fix; and the regression test with its expected pre-fix
failure. State "no actionable findings" and list reviewed files/tests if the assigned
surface is clean. Omit naming/style preferences.
```

Assign non-exclusive lenses so load-bearing seams receive overlapping scrutiny:

1. **Facade and dependency architecture**
   - `aws_ecs_acceptance.py`, `__init__.py`, `contracts.py`, `state.py`, `http_client.py`, `capture.py`
   - facade/parser/dispatch/export tests, runbook contract, and dependency guard
   - look for import-time effects, identity breaks, parser/dispatch drift, patch-target mistakes, error/stdout changes, upward edges, cycles, and packaged-module omissions.
2. **Protected mutation and concurrency**
   - `secure_documents.py`, `state.py`, `manifest.py`, `receipt_store.py`, `approvals.py`
   - owning tests plus callers in control/cleanup
   - look for lock-scope narrowing, TOCTOU/symlink/inode mistakes, mode/durability gaps, lost updates, partial publication/indexing, authority-binding errors, and non-idempotent recovery.
3. **Schemas, finalization, and task admission**
   - `receipt_contracts.py`, `manifest_schema.py`, `scenario_inventory.py`, `task_definition.py`
   - owning tests plus every manifest mutator
   - look for closed-field/hash/binding gaps, finalization bypasses, validation/mutation dependency inversions, malformed boundary values, two-model provider inference mistakes, and incorrect secret admission.
4. **AWS I/O and bounded effects**
   - `s3.py`, `bedrock.py`, `orphan_sweep.py`, plus direct `http_client.py`/`capture.py` seams
   - owning tests and facade dispatch
   - look for AWS retry/pagination/item/not-found mistakes, partial/interrupted effects, wrong ownership proof, redaction leaks, record-selection drift, publication-before-cleanup violations, and unsafe client mocking assumptions.
5. **Telemetry identity and outage behavior**
   - `operator_telemetry.py` plus direct Landscape/HTTP/AWS seams
   - telemetry, connection-budget, and Landscape boundary tests
   - look for stale wall-clock trace identity, inconsistent `started_at` caching, dimension drift, query bounds, outage/success confusion, forbidden content, and connection-budget arithmetic errors.
6. **Evidence, ledger, cleanup, and control state machines**
   - `evidence.py`, `gate_ledger.py`, `cleanup.py`, `control_service.py`
   - owning tests plus their lower-layer calls
   - look for prepare/commit order violations, premature `cleanup_required` clearing, terminal-record/hash mismatch, incomplete crash recovery, post-commit writes, stale reads, replay mutation, and missing closed-schema checks.
7. **Test integrity and adversarial integration gaps**
   - all new production modules and split tests, with emphasis on cross-module seams
   - normalized identity/coverage evidence, architecture tests, wheel/container import contract
   - look for moved tests that patch the facade instead of the lookup owner, lost/duplicated assertions, falsely passing mocks, call-signature/exception-translation drift, uncovered exception branches, dead duplicate implementations, missing concurrency/adversarial cases, and tests that no longer exercise production paths.

Expected: seven independent reports tied to concrete current code. Reviewers do not coordinate conclusions before returning. A reviewer failure or incomplete assigned surface is rerun; silence is not a clean result.

- [ ] **Step 3: Validate and triage every finding before editing**

The primary executor assigns stable IDs `FP-001`, `FP-002`, and so on, deduplicates findings by failure mechanism, inspects the cited code, and runs the narrowest reproduction against unchanged `REVIEW_SHA`. Classify each as:

- **Scoped defect:** introduced by the extraction or located in the refactored controller/package and material to its stated behavior. It must be fixed in this task.
- **False positive:** disproved by code/test evidence. Record the reason in the Filigree review summary.
- **Unrelated pre-existing defect:** outside the fixed paths or behavior of this refactor. Create a proper Filigree issue with evidence and dependency only if it truly is outside scope; do not use an expiring observation and do not edit unrelated code here.

Any unresolved scoped correctness, security, data-integrity, CLI compatibility, or material test-integrity finding blocks Task 14. Do not downgrade a scoped defect to an observation to finish the plan.

Before validation begins, require `git rev-parse HEAD^{commit}` and `HEAD^{tree}` to equal the recorded `REVIEW_SHA` and `REVIEW_TREE`. If not, discard the first-wave reports and restart Step 1.

```bash
set -Eeuo pipefail
REVIEW_SHA=$(<.elspeth/aws-ecs-acceptance-refactor/review-sha.txt)
REVIEW_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/review-tree.txt)
test "$(git rev-parse HEAD^{commit})" = "$REVIEW_SHA"
test "$(git rev-parse HEAD^{tree})" = "$REVIEW_TREE"
```

- [ ] **Step 4: Fix validated scoped defects serially with regressions**

For each independent failure mechanism:

1. Add the smallest focused test that fails for the reported scenario.
2. Run it and confirm the expected failure for the expected reason.
3. Apply the smallest fix in the owning module; do not perform adjacent cleanup.
4. Run the focused owner tests, facade contract, and architecture guard.
5. Run the common extraction gate. Run the milestone gate for concurrency, finalization, receipt, cleanup, facade, or cross-module changes.
6. Stage only the test and source paths for that fix, inspect the staged diff, and commit with `fix(web): <specific failure mechanism>`.
7. Require a clean worktree before the next fix.

Findings that share one inseparable fix surface may share a commit; otherwise keep separate regression/fix commits so rollback remains safe. Review agents remain read-only throughout—only the primary executor writes, preventing conflicting cleanup edits.

- [ ] **Step 5: Recheck fixes and run a fresh clean-room review wave**

Send each repaired finding back to its original reviewer with the current full SHA, stable finding ID, fix commit, and narrow regression node. Require `CLOSED` with checked evidence or `RESIDUAL` with a concrete remaining failure. A residual returns to Steps 3-5.

Then require a clean worktree and capture `CLEANROOM_SHA=$(git rev-parse HEAD^{commit})` and `CLEANROOM_TREE=$(git rev-parse HEAD^{tree})` in the ignored evidence directory. Spawn three fresh read-only subagents with `fork_turns="none"`; they must not have proposed or implemented fixes and must not receive first-pass reports. Give each a self-contained prompt containing only absolute `/home/john/elspeth/AGENTS.md`, the approved design, this plan, assigned current files, and `CLEANROOM_SHA` until its independent report returns. Each reviewer confirms the SHA before and after review and uses a reviewer-unique pytest temp directory with the cache provider disabled. After all three return, require current `HEAD` and tree to equal `CLEANROOM_SHA` and `CLEANROOM_TREE` before triaging their reports.

```bash
set -Eeuo pipefail
test -z "$(git status --porcelain)"
CLEANROOM_SHA=$(git rev-parse HEAD^{commit})
CLEANROOM_TREE=$(git rev-parse HEAD^{tree})
printf '%s\n' "$CLEANROOM_SHA" > .elspeth/aws-ecs-acceptance-refactor/cleanroom-sha.txt
printf '%s\n' "$CLEANROOM_TREE" > .elspeth/aws-ecs-acceptance-refactor/cleanroom-tree.txt
# Dispatch and await all three read-only reviewers here.
CLEANROOM_SHA=$(<.elspeth/aws-ecs-acceptance-refactor/cleanroom-sha.txt)
CLEANROOM_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/cleanroom-tree.txt)
test "$(git rev-parse HEAD^{commit})" = "$CLEANROOM_SHA"
test "$(git rev-parse HEAD^{tree})" = "$CLEANROOM_TREE"
```

1. one clean-room reviewer reads every current private-package module, facade, and focused test in full;
2. one concurrency/recovery reviewer rechecks protected documents, manifests, receipt storage, approvals, ledger, and cleanup seams; and
3. one compatibility/integration reviewer rechecks CLI dispatch, exports, AWS/telemetry boundaries, exception translation, test lookup ownership, and packaging imports.

Use the common finding format. Across the wave they must explicitly check:

- every target module exists exactly once and no moved implementation remains duplicated in the facade;
- facade exports are identical to their owner objects and all dispatch paths resolve;
- layer/forbidden-edge rules hold in actual imports;
- lock/finalization/provider/telemetry/receipt/cleanup invariants cross module boundaries intact;
- reviewer-driven tests exercise production lookup owners rather than stale facade aliases; and
- no validated first-pass failure scenario remains reproducible.

If the clean-room wave finds a scoped defect, return to Steps 3-5 and repeat a fresh clean-room wave after correction. Do not proceed with an unresolved finding.

- [ ] **Step 6: Close the cleanup milestone**

```bash
set -Eeuo pipefail
# Run the common extraction gate and milestone gate one more time.
test -z "$(git status --porcelain)"
REVIEW_SHA=$(<.elspeth/aws-ecs-acceptance-refactor/review-sha.txt)
REVIEW_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/review-tree.txt)
test "$(git rev-parse "${REVIEW_SHA}^{tree}")" = "$REVIEW_TREE"
POST_REVIEW_SHA=$(git rev-parse HEAD^{commit})
POST_REVIEW_TREE=$(git rev-parse HEAD^{tree})
git merge-base --is-ancestor "$REVIEW_SHA" "$POST_REVIEW_SHA"
filigree add-comment "$PARENT_ID" \
  "Post-split review complete: initial REVIEW_SHA=$REVIEW_SHA final POST_REVIEW_SHA=$POST_REVIEW_SHA POST_REVIEW_TREE=$POST_REVIEW_TREE; seven parallel lenses plus three fresh clean-room reviews completed; no unresolved scoped findings." \
  --actor codex-aws-ecs
```

Record validated finding counts, false-positive dispositions, created unrelated issue IDs, fix commits, focused reproductions, and reviewer recheck results in the same Filigree comment or an immediately following comment. Do not create a repository review receipt or sidecar.

If there were no validated fixes, require `POST_REVIEW_SHA == REVIEW_SHA`; do not create a review-only commit.

**Definition of Done:** seven independent reviewers have examined every smaller module and seam from first principles; every scoped defect has a regression and fix; repaired findings were rechecked by their reporters; three fresh clean-room reviewers report no unresolved scoped findings; common/milestone gates pass on a clean tree.

---

### Task 14: Run final local acceptance and hand off the frozen source

**Files:**

- Verify only: `src/elspeth/web/aws_ecs_acceptance.py`
- Verify only: `src/elspeth/web/_aws_ecs_acceptance/`
- Verify only: `tests/unit/web/aws_ecs_acceptance/`
- Verify unchanged: `docs/runbooks/aws-ecs-deployment.md`
- Verify unchanged: `uv.lock`
- Record evidence on: the single Filigree parent

Do not modify source while executing this task. If a test-only correction is necessary, commit it separately, rerun this entire task from Step 1, and produce a new frozen SHA/tree tuple.

Before running any Task 14 verification, pin the candidate being tested:

```bash
set -Eeuo pipefail
test -z "$(git status --porcelain)"
CANDIDATE_SHA=$(git rev-parse HEAD^{commit})
CANDIDATE_TREE=$(git rev-parse HEAD^{tree})
printf '%s\n' "$CANDIDATE_SHA" > .elspeth/aws-ecs-acceptance-refactor/candidate-sha.txt
printf '%s\n' "$CANDIDATE_TREE" > .elspeth/aws-ecs-acceptance-refactor/candidate-tree.txt
```

- [ ] **Step 1: Re-run focused compatibility, architecture, identity, and coverage**

Run the common extraction gate and milestone gate. Then:

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen python -m elspeth.web.aws_ecs_acceptance \
  scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A \
  | tee .elspeth/aws-ecs-acceptance-refactor/final-scenario-namespace.txt
test "$(<.elspeth/aws-ecs-acceptance-refactor/final-scenario-namespace.txt)" = a-f8b447a7b5b51f38c800
git diff --exit-code "$BASE_SHA" -- docs/runbooks/aws-ecs-deployment.md
base_runbook_calls=$(git show "$BASE_SHA:docs/runbooks/aws-ecs-deployment.md" \
  | rg -o 'python -m elspeth\.web\.aws_ecs_acceptance' | wc -l)
current_runbook_calls=$(rg -o 'python -m elspeth\.web\.aws_ecs_acceptance' \
  docs/runbooks/aws-ecs-deployment.md | wc -l)
test "$current_runbook_calls" -eq "$base_runbook_calls"
```

Expected: facade, dispatcher, normalized node identities, dynamic controller/package coverage, deterministic probe, and unchanged runbook all pass against the selected base.

- [ ] **Step 2: Run repository lint, type, and locally owned source-sensitive gates**

Run the complete fail-fast command block from Task 0 Step 4 against the final tree. Then run:

```bash
set -Eeuo pipefail
git diff --exit-code "$BASE_SHA" -- uv.lock docs/runbooks/aws-ecs-deployment.md
git diff --check "$BASE_SHA"..HEAD
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/architecture/test_sink_publication_callers.py \
  tests/unit/web/test_landscape_access_guard.py -q
```

Expected: every locally owned source/static gate passes. Checks that consume out-of-band release metadata are not part of this source-only acceptance.

- [ ] **Step 3: Run Python 3.12 and canonical Python 3.13 coverage lanes**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --isolated --python 3.12 --frozen --all-extras pytest tests/ \
  -v -m "not slow and not stress and not performance and not testcontainer and not live_aws and not fingerprint_baseline"
env -u VIRTUAL_ENV uv run --frozen pytest tests/ \
  --cov=src/elspeth --cov-report=xml --cov-report=term-missing --cov-fail-under=85 \
  -v -m "not slow and not stress and not performance and not testcontainer and not live_aws and not fingerprint_baseline"
coverage_status=0
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/core/landscape/*' --fail-under=92 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/core/canonical.py' --fail-under=99 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/engine/orchestrator/*' --fail-under=90 || coverage_status=1
env -u VIRTUAL_ENV uv run --frozen coverage report --include='src/elspeth/contracts/*' --fail-under=62 || coverage_status=1
test "$coverage_status" -eq 0
```

Expected: compatibility lane, aggregate coverage, and all four subsystem floors pass. The negative marker exclusion is the boundary for a release-metadata-dependent test owned by the out-of-band release process; do not update its fixture here.

- [ ] **Step 4: Run focused and complete PostgreSQL testcontainers**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest -m testcontainer \
  tests/testcontainer/web/test_doctor_aws_ecs_postgres.py \
  tests/testcontainer/web/test_schema_probe_postgres.py \
  tests/testcontainer/web/test_aws_ecs_validate_only_startup.py \
  tests/testcontainer/web/test_aws_ecs_readiness_postgres.py \
  tests/testcontainer/web/test_landscape_write_gate_postgres.py -q
env -u VIRTUAL_ENV uv run --frozen pytest tests/testcontainer/ -v -m testcontainer
```

Expected: the five AWS startup/readiness proofs and complete PostgreSQL contention suite pass against real PostgreSQL. A missing Docker daemon is not a clean result.

- [ ] **Step 5: Build and smoke the installed wheel**

```bash
set -Eeuo pipefail
task_tmp=$(mktemp -d)
env -u VIRTUAL_ENV uv build --out-dir "$task_tmp/dist"
uv venv "$task_tmp/venv" --python 3.13
wheel_path=$(find "$task_tmp/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)
test -n "$wheel_path"
uv pip install --python "$task_tmp/venv/bin/python" \
  "${wheel_path}[webui,llm,aws,postgres]"
"$task_tmp/venv/bin/python" - <<'PY'
import boto3
import psycopg
import psycopg2
from sqlalchemy import create_engine
import elspeth.web.aws_ecs_acceptance as acceptance

assert create_engine("postgresql://u:p@localhost/db").dialect.driver == "psycopg2"
assert create_engine("postgresql+psycopg://u:p@localhost/db").dialect.driver == "psycopg"
assert callable(acceptance.main)
PY
"$task_tmp/venv/bin/python" -m elspeth.web.aws_ecs_acceptance --help >/dev/null
probe=$("$task_tmp/venv/bin/python" -m elspeth.web.aws_ecs_acceptance \
  scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A)
test "$probe" = a-f8b447a7b5b51f38c800
```

Expected: publishable extras install without workspace-only extras; boto3, both PostgreSQL drivers, both SQLAlchemy dialect selections, acceptance import, help, and deterministic probe pass.

- [ ] **Step 6: Build and smoke the local container**

```bash
set -Eeuo pipefail
CANDIDATE_SHA=$(<.elspeth/aws-ecs-acceptance-refactor/candidate-sha.txt)
CANDIDATE_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/candidate-tree.txt)
test "$(git rev-parse HEAD^{commit})" = "$CANDIDATE_SHA"
test "$(git rev-parse HEAD^{tree})" = "$CANDIDATE_TREE"
local_image="elspeth:aws-ecs-acceptance-refactor-${CANDIDATE_SHA:0:12}"
test -z "$(git status --porcelain)"
docker buildx build \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=$CANDIDATE_SHA" \
  --load -t "$local_image" .
test "$(docker image inspect "$local_image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$CANDIDATE_SHA"
test "$(docker image inspect "$local_image" --format '{{ index .Config.Labels "io.elspeth.install-extras" }}')" = "webui llm aws postgres"
docker run --rm "$local_image" --version
docker run --rm -i --entrypoint python "$local_image" - <<'PY'
from pathlib import Path
import boto3
import psycopg
import psycopg2
from sqlalchemy import create_engine
import elspeth.web
import elspeth.web.aws_ecs_acceptance as acceptance

assert create_engine("postgresql://u:p@localhost/db").dialect.driver == "psycopg2"
assert create_engine("postgresql+psycopg://u:p@localhost/db").dialect.driver == "psycopg"
assert callable(acceptance.main)
root = Path(elspeth.web.__file__).parent
assert (root / "frontend" / "dist" / "index.html").is_file()
PY
docker run --rm --entrypoint python "$local_image" \
  -m elspeth.web.aws_ecs_acceptance --help >/dev/null
probe=$(docker run --rm --entrypoint python "$local_image" \
  -m elspeth.web.aws_ecs_acceptance scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A)
test "$probe" = a-f8b447a7b5b51f38c800
```

Expected: local image records the exact source SHA and install extras; both drivers/dialects, package import, frontend asset, CLI help, and deterministic probe pass. This is a local packaging test, not an image publication or ECS deployment.

- [ ] **Step 7: Prove the complete dependency shape**

Run the architecture test against the implementation checkout and import every target module in a fresh interpreter:

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py -q
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import importlib

modules = (
    "contracts", "secure_documents", "state", "http_client", "capture",
    "receipt_contracts", "s3", "bedrock", "operator_telemetry",
    "manifest_schema", "scenario_inventory", "manifest", "task_definition",
    "orphan_sweep", "receipt_store", "approvals", "evidence", "gate_ledger",
    "cleanup", "control_service",
)
for name in modules:
    importlib.import_module(f"elspeth.web._aws_ecs_acceptance.{name}")
PY
```

Expected: the authoritative AST graph sees the exact target module set, no private-package cycle, no private-to-facade import, and no forbidden/upward edge; every module imports successfully without side effects. Facade characterization proves `main` remains the sole CLI entry point.

- [ ] **Step 8: Freeze, record, and stop**

```bash
set -Eeuo pipefail
test -z "$(git status --porcelain)"
CANDIDATE_SHA=$(<.elspeth/aws-ecs-acceptance-refactor/candidate-sha.txt)
CANDIDATE_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/candidate-tree.txt)
test "$(git rev-parse HEAD^{commit})" = "$CANDIDATE_SHA"
test "$(git rev-parse HEAD^{tree})" = "$CANDIDATE_TREE"
test "$(<.elspeth/aws-ecs-acceptance-refactor/base-sha.txt)" = "$BASE_SHA"
BASE_TREE=$(<.elspeth/aws-ecs-acceptance-refactor/base-tree.txt)
test "$(git rev-parse "${BASE_SHA}^{tree}")" = "$BASE_TREE"
test "$(git ls-remote origin refs/heads/release/0.7.2 | awk '{print $1}')" = "$BASE_SHA"
git fetch --no-tags origin refs/heads/release/0.7.2
test "$(git rev-parse FETCH_HEAD^{commit})" = "$BASE_SHA"
test "$(git ls-remote origin refs/heads/release/0.7.2 | awk '{print $1}')" = "$BASE_SHA"
git merge-base --is-ancestor "$BASE_SHA" HEAD
test "$(git rev-list --min-parents=2 --count "$BASE_SHA..HEAD")" -eq 0
FROZEN_SOURCE_SHA=$CANDIDATE_SHA
FROZEN_SOURCE_TREE=$CANDIDATE_TREE
git log --reverse --format='%H %s' "$BASE_SHA..$FROZEN_SOURCE_SHA" \
  | tee .elspeth/aws-ecs-acceptance-refactor/frozen-commit-range.txt
test -s .elspeth/aws-ecs-acceptance-refactor/frozen-commit-range.txt
while IFS= read -r commit_line; do
  filigree add-comment "$PARENT_ID" \
    "Refactor commit: $commit_line" --actor codex-aws-ecs
done < .elspeth/aws-ecs-acceptance-refactor/frozen-commit-range.txt
filigree add-comment "$PARENT_ID" \
  "Final local gate summary at $FROZEN_SOURCE_SHA: focused facade/controller/runbook/architecture PASS; locally owned static gates PASS; Python 3.12 compatibility PASS; Python 3.13 coverage and subsystem floors PASS; focused and complete PostgreSQL testcontainers PASS; installed wheel PASS; local container PASS; exact dependency graph/import smoke PASS." \
  --actor codex-aws-ecs
test "$(git rev-parse HEAD^{commit})" = "$FROZEN_SOURCE_SHA"
test "$(git rev-parse HEAD^{tree})" = "$FROZEN_SOURCE_TREE"
test -z "$(git status --porcelain)"
filigree add-comment "$PARENT_ID" \
  "Frozen source handoff: BASE_SHA=$BASE_SHA BASE_TREE=$BASE_TREE FROZEN_SOURCE_SHA=$FROZEN_SOURCE_SHA FROZEN_SOURCE_TREE=$FROZEN_SOURCE_TREE. Tasks 0-14 local source-only gates passed; first-principles cleanup review has no unresolved scoped findings; ordered commit range is recorded in the preceding parent comments." \
  --actor codex-aws-ecs
filigree close "$PARENT_ID" \
  --reason "Source-only refactor locally verified and handed off at $FROZEN_SOURCE_SHA" \
  --commit "$IMPLEMENTATION_BRANCH@$FROZEN_SOURCE_SHA" \
  --expected-assignee codex-aws-ecs --actor codex-aws-ecs
```

Expected: the parent records the selected base, frozen commit/tree, commit range, and green local evidence, then closes at the source handoff. Stop here. Do not create a repository handoff artifact, modify any file, publish the branch, wait for remote checks, or perform downstream release work.

Any later source or test correction invalidates this handoff. Commit the correction, rerun the affected Task 13 reviewer recheck and every step in Task 14, and issue a new frozen SHA/tree/evidence tuple.

**Definition of Done:** one clean frozen source tip is fully locally verified, its immutable identity/evidence is handed to the release owner, and no out-of-band release activity has been performed.

---

## Final acceptance checklist

- [ ] The selected `BASE_SHA` was explicit, remote-current at start, present in history, and green before movement.
- [ ] One Filigree parent tracked the refactor; no per-commit issue hierarchy or review sidecar was created.
- [ ] The facade retains executable CLI behavior, every characterized dispatch leaf, safe stdout/stderr/error behavior, and public re-export identity.
- [ ] Baseline normalized test identities remain present and focused controller/package coverage did not regress from the selected base.
- [ ] Protected-document locking covers complete cross-process read-modify-write transactions with exact file protections and durable atomic publication.
- [ ] Finalized control manifests reject every mutator; committed cleanup replay is validation-only.
- [ ] Both composer model providers drive exact OpenRouter secret admission.
- [ ] Operator trace IDs use durable Landscape `started_at` across all adapters and retries.
- [ ] Receipt publication plus indexing remains one locked authority.
- [ ] Cleanup prepare/commit order, interruption recovery, terminal hashes, and replay behavior remain unchanged.
- [ ] Seven parallel first-principles review lenses and three fresh clean-room reviews completed; every scoped finding has a regression/fix/recheck and none remains unresolved.
- [ ] The authoritative AST graph has the exact module set, correct layer direction, no load-bearing forbidden edge, and no private-package cycle; every private module imports without side effects.
- [ ] Ruff, formatting, mypy, locally owned static gates, Python 3.12/3.13 lanes, focused/full PostgreSQL testcontainers, wheel smoke, and local container smoke pass.
- [ ] `uv.lock` and the tracked runbook are unchanged from the selected base.
- [ ] The Filigree parent comments record `BASE_SHA`, `BASE_TREE`, `FROZEN_SOURCE_SHA`, `FROZEN_SOURCE_TREE`, the ordered commit range, and the concrete local verification result.
- [ ] Work stopped at frozen-source handoff.
