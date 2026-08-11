# State Engine Pinning and Completion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Pin ELSPETH's current durable state-engine contract in a versioned, machine-validated proof catalog and close every mandatory implementation and evidence cell needed for a current-baseline `complete` verdict.

**Architecture:** Treat pinning and completion as two distinct gates. First reconcile current source, supported deployment profiles, state vocabulary, plugin inventory, and compiler-facing runtime obligations into the frozen v2 catalog; then execute bounded implementation/proof cohorts and publish a v3 catalog whose explicit applicability and provider-variant cases can derive the final complete verdict without a false Cartesian product. Preserve production authority: tests proceed from real orchestration boundaries inward, independent-process claims use operating-system processes, PostgreSQL claims use a real PostgreSQL 16 service, and missing provider credentials remain `unknown` rather than being replaced by mocks.

**Tech Stack:** Python 3.12+, pytest/xdist, Hypothesis, SQLAlchemy, SQLite WAL, PostgreSQL 16/testcontainers, Filigree, Loomweave, Warpline, Wardline, and the existing Landscape/scheduler/orchestrator packages.

**Prerequisites:**

- Base all implementation work on a clean, current `release/0.7.2` worktree.
- Read `AGENTS.md`, `docs/agents/recent-code-hints.md`, ADR-026, ADR-029, ADR-030, ADR-038, and every document under `docs/architecture/state_engine/` before editing code.
- Use `PYTHONPATH=<worktree>/src <venv>/bin/python ...` in ordinary shared-venv worktrees and verify `elspeth.__file__` before trusting a run.
- Build the final assessment in a fresh clean worktree with a worktree-local environment created by `uv sync --frozen --all-extras`; the final evidence package must not use a symlinked environment.
- Never obtain an operator signing key. Trust-tier verification remains key-free and baseline-relative.
- Current planning baseline: `release/0.7.2` at `4e8042d266568d277b6546f2d1bbe7fe79891556`.

---

## Current evidence and decisions

The current state-engine hub is not a current release verdict. It points to the 2026-07-18 assessment at `3c782ac3c7efb0550495be38f75800eddffa639a`, reports 68 v1 legs, 44 gaps, 24 unknowns, zero confirmed legs, and ten open hard gates. Since that baseline, current source has changed across every major state-engine boundary.

The v1 catalog cannot be completed honestly against current source because it is already normatively stale:

- ADR-038 and current source add the non-terminal `ABANDONED` fate, its fenced run-finalization producer, accounting semantics, and PostgreSQL lock-order obligation.
- Row-union barriers now persist scheduler context and execute through durable barrier/recovery paths that v1 does not name.
- The live plugin inventory has grown from 7/31/8 to 9/33/9 source/transform/sink plugins.
- v1 names only `sqlite-wal`, while maintained AWS deployment material configures a PostgreSQL Landscape database. ADR-030 still says PostgreSQL runtime is unsupported. This contradiction opens `HG-10-normative-contract-drift` before any behavioral test runs.
- Warpline's current edge snapshot is 1,007 commits behind the planning baseline. Its worklist is advisory and incomplete until a fresh snapshot is captured.
- Six live `[state engine]` Filigree tasks remain open: `elspeth-c0d4a28e11`, `elspeth-9cd07962c7`, `elspeth-9a52eb80f9`, `elspeth-2aba594afb`, `elspeth-2e66723070`, and `elspeth-6f6bbbec00`.

The default support decision for this plan is:

1. SQLite WAL remains required for single-process and one-host leader/follower packs.
2. PostgreSQL 16 is a required first-class backend for the maintained single-leader Landscape profile used by AWS deployment.
3. Multi-host/multi-replica scheduling remains explicitly unsupported until its separate program changes that claim; a PostgreSQL race test does not by itself advertise multi-replica support.
4. All current first-party plugins remain in the production-supported inventory. Provider-backed plugins require real provider acceptance for a production-supported verdict; absence of credentials is `unknown`.
5. Existing v1 leg IDs remain stable. v2 adds explicit obligations for `ABANDONED` and row-union behavior rather than silently folding them into old prose.

PostgreSQL support is not an execution fork: Task 1 must include the maintained AWS PostgreSQL 16 single-leader profile in the state-engine catalog. The implementation must never retain that production claim while excluding PostgreSQL from the state-engine catalog.

## Completion contract

“Pinned” means all of the following are true at one exact baseline:

- one accepted support-profile ADR agrees with deployment documentation and source;
- one versioned catalog names every durable state/subtype, transition, boundary, read model, forbidden path, store, deployment, lifecycle, and first-party plugin;
- the catalog validator proves exact vocabulary and inventory agreement with source;
- a full assessment materializes every required cell and gives every unresolved cohort a live owner and observable exit gate;
- the compiler-facing handoff can bind to a stable `catalog_id`, schema epoch, and catalog digest without importing assessment prose.

“Complete” means:

- every required catalog cell is `pass` or catalog-approved `not_applicable`;
- no required cell is `partial`, `fail`, or `unknown`;
- every hard gate is closed with current executable support;
- all supported backends, deployment shapes, lifecycle modes, and first-party plugin boundaries are covered;
- the exact maintained selectors run in CI or the release gate;
- the full assessment package passes direct validation and independent architecture, evidence, and reproducibility review.

## Dependency shape

| Stage | Work | Dependency | Parallelism |
| --- | --- | --- | --- |
| A | Support-profile decision and v2 contract | None | Sequential |
| B | Current full rebaseline and live ownership | A | Sequential |
| C1 | Queue, source, transform, and gate cohorts | B | Parallel with C2-C5 |
| C2 | Leases, coordination, multiprocess, and read models | B | Parallel with C1/C3-C5 |
| C3 | Aggregation, coalesce, and row-union barriers | B | Parallel with C1/C2/C4/C5 |
| C4 | Sink effects and post-sink repair | B | Parallel with C1-C3/C5 |
| C5 | Run lifecycle, `ABANDONED`, follower, and plugin profiles | B | Parallel with C1-C4 |
| D | Full assessment, CI/release gate, and compiler handoff | All C cohorts | Sequential |

Every dated assessment is immutable after publication. Task 3 creates the
current full pinning assessment. Each C-stage cohort creates a new dated delta
assessment that names its parent, changed cells, changed gates, and freshly
executed evidence. Task 12 creates a new full assessment at the frozen final
commit; it never edits the Task 3 package or any intermediate delta.

### Retained pytest evidence rule

The file-level and `-k` commands in task RED/GREEN steps are development
selectors, not promotable evidence records. Before promoting a cell, replace
them with an explicit, reviewed list of full pytest node IDs containing `::`
and execute exactly those nodes. Each profile-bound pytest process must retain:

- the exact node list (`.nodes`) and exact argv/environment recorded in the manifest;
- one JUnit XML written with `--junitxml`;
- one runtime profile report written by `scripts.state_engine_profile_reporter`;
- captured stdout and stderr with secrets and connection URLs excluded;
- one genuinely exercised profile only, observed through `observe_sqlite` or
  `observe_postgresql` on the live connection used by the test.

The JUnit, profile report, and node list must contain the same exact node IDs.
Skipped, deselected, uncollected, xfailed, xpassed, warning-bearing, or
profile-mismatched runs are retained as non-pass evidence. Run both
`validate-package` and `collect-evidence` before any delta is published.

### Test cadence

Do not run the full `pytest tests/` suite during Tasks 1–11. Use directly
affected focused selectors per implementation slice, one bounded family or
contract cohort per task, and PostgreSQL/live-provider lanes only at their
explicit integration milestones. Task 12 runs the CI-equivalent full suite once
after all implementation churn is frozen; rerun it only if that final gate
uncovers a fix that changes the frozen commit.

---

### Task 1: Reconcile and pin supported state-engine profiles

**Files:**

- Create: `docs/architecture/adr/041-state-engine-supported-profiles.md`
- Modify: `docs/architecture/adr/README.md`
- Modify: `docs/architecture/adr/030-multi-worker-deployment-shape.md`
- Modify: `deploy/aws-ecs/terraform/README.md`
- Create: `tests/unit/architecture/test_state_engine_supported_profiles.py`

**Step 1: Write the failing support-profile contract test**

Create a test that loads the new ADR and the forthcoming v2 catalog, then pins the exact currently advertised profiles:

```python
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
ADR = ROOT / "docs/architecture/adr/041-state-engine-supported-profiles.md"


def test_state_engine_profiles_match_current_product_claim() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    stores = {item["id"]: item for item in catalog["execution_profiles"]["state_store"]}

    assert set(stores) == {"sqlite-wal", "postgresql-16"}
    assert stores["sqlite-wal"]["required"] is True
    assert stores["postgresql-16"]["required"] is True
    assert "multi-host" not in catalog["execution_profiles"]["deployments"]
    assert ADR.is_file()
    assert "PostgreSQL 16 single-leader" in ADR.read_text(encoding="utf-8")
```

**Step 2: Run the test and verify RED**

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/architecture/test_state_engine_supported_profiles.py
```

Expected: failure because the v2 catalog and ADR-041 do not exist.

**Step 3: Write ADR-041 and reconcile ADR-030**

ADR-041 must state, without aspirational ambiguity:

- SQLite WAL one-host leader/follower support remains governed by ADR-030.
- PostgreSQL 16 is a required first-class state-engine backend for the maintained single-leader Landscape deployment.
- PostgreSQL multi-replica scheduling remains unsupported and has a separate owner.
- DB-server time, row locking, isolation, schema migration, and connection-loss behavior are part of the PostgreSQL state-engine contract.
- Any future multi-replica enablement requires a new catalog/profile revision and cannot inherit the single-leader evidence.

Mark ADR-030 as amended by ADR-041 rather than rewriting its historical 0.6.0 decision. Reconcile the AWS README to use the same vocabulary.

**Step 4: Create the minimal v2 catalog shell**

Copy v1 to `proof-catalog/v2/catalog.json`, change only the catalog identity, support-profile skeleton, and references needed for this test. Task 2 owns the full leg and case expansion.

**Step 5: Run the focused test and documentation link check**

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/architecture/test_state_engine_supported_profiles.py
git diff --check
```

Expected: one passing test and no whitespace errors.

**Step 6: Commit**

```bash
git add docs/architecture/adr/041-state-engine-supported-profiles.md \
  docs/architecture/adr/README.md \
  docs/architecture/adr/030-multi-worker-deployment-shape.md \
  deploy/aws-ecs/terraform/README.md \
  docs/architecture/state_engine/proof-catalog/v2/catalog.json \
  tests/unit/architecture/test_state_engine_supported_profiles.py
git commit -m "docs(state-engine): pin supported runtime profiles"
```

**Definition of Done:**

- [ ] PostgreSQL 16 is included as a required first-class state-engine backend for the maintained AWS single-leader Landscape deployment.
- [ ] SQLite one-host and PostgreSQL single-leader scopes are mechanically distinct.
- [ ] Multi-replica remains explicitly out of scope.
- [ ] The focused test fails before the change and passes afterward.

---

### Task 2: Define the v2 catalog and make drift mechanically impossible

**Files:**

- Modify: `docs/architecture/state_engine/proof-catalog/v2/catalog.json`
- Modify: `docs/architecture/state_engine/proof-catalog/README.md`
- Modify: `docs/architecture/state_engine/completeness-criteria.md`
- Modify: `docs/architecture/state_engine/assessment-program.md`
- Create: `scripts/state_engine_assessment.py`
- Create: `tests/unit/architecture/test_state_engine_catalog_contract.py`

**Step 1: Write failing catalog tests**

The test must use the production plugin discovery surface and owned enum values, not duplicate hand-maintained Python lists:

```python
from __future__ import annotations

import json
from pathlib import Path

from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.plugins.infrastructure.discovery import discover_all_plugins

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"


def test_v2_catalog_matches_owned_vocabularies_and_plugins() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert catalog["catalog_id"] == "elspeth-state-engine-v2"
    assert catalog["vocabularies"]["token_work_status"] == [item.value for item in TokenWorkStatus]
    assert catalog["vocabularies"]["terminal_path"] == [item.value for item in TerminalPath]

    live = {
        kind: sorted(plugin.name for plugin in plugins)
        for kind, plugins in discover_all_plugins().items()
    }
    profile = catalog["execution_profiles"]["first_party_plugins"]
    assert {kind: profile[kind] for kind in live} == live


def test_v2_names_new_durable_contracts() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    legs = {leg["id"] for leg in catalog["legs"]}
    assert {"TS-19", "PB-10", "RM-14", "F-14"} <= legs
```

**Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/architecture/test_state_engine_catalog_contract.py
```

Expected: failures because the v2 vocabulary, current plugin inventory, and new legs are incomplete.

**Step 3: Complete the v2 catalog**

Retain all v1 IDs and add these stable obligations:

- `TS-19`: fenced non-resumable run finalization records `(NULL, ABANDONED)` atomically with the terminal run stamp.
- `PB-10`: row-union arrival, loss, timeout, release, late-arrival, and recovery behavior through the production executor.
- `RM-14`: run accounting and resume distinguish decided, resumable pending, abandoned, and contradictory decided-plus-abandoned tokens.
- `F-14`: `ABANDONED` cannot enter processing, resume re-derivation, predicate counters, or coexist with a decided terminal outcome.

Give `PB-10` named required cases rather than one broad cell:

```json
{
  "id": "PB-10",
  "family": "production_boundary",
  "title": "Row-union barrier",
  "contract": "Durable per-row branch membership converges exactly once across arrival, loss, timeout, release, late arrival, and restart.",
  "applicability_profile": "all-required-v2",
  "required_cases": [
    "all-branches-arrive",
    "branch-loss-before-first-arrival",
    "branch-loss-after-partial-arrival",
    "timeout",
    "late-arrival-after-release",
    "restart-before-release",
    "restart-after-release-before-sink"
  ]
}
```

Add backend/profile applicability as named cases. A PostgreSQL test may satisfy only the PostgreSQL case; it must not promote the SQLite case or vice versa.

**Step 4: Extract the direct validator into a tested script**

`scripts/state_engine_assessment.py` must provide these subcommands:

```text
validate-catalog <catalog.json>
init-full <assessment-id> <output-directory>
validate-package <assessment.json>
collect-evidence <assessment.json>
check-links
```

Implement duplicate-key-safe loading as a public helper:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_unique_json(path: Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value
```

Move the existing embedded validation behavior into functions without weakening any check. Keep `assessment-program.md` as the runnable operator guide, but make it call the script rather than carrying a second validator implementation.

**Step 5: Run RED/GREEN validator tests**

Include fixtures for duplicate keys, missing legs, stale plugin inventory, invalid profile references, dangling evidence, false derived totals, and a complete minimal manifest.

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/architecture/test_state_engine_catalog_contract.py
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-catalog docs/architecture/state_engine/proof-catalog/v2/catalog.json
```

Expected: all tests pass and the CLI prints `state-engine catalog: valid`.

**Step 6: Commit**

```bash
git add docs/architecture/state_engine/proof-catalog \
  docs/architecture/state_engine/completeness-criteria.md \
  docs/architecture/state_engine/assessment-program.md \
  scripts/state_engine_assessment.py \
  tests/unit/architecture/test_state_engine_catalog_contract.py
git commit -m "feat(state-engine): define and validate the v2 proof contract"
```

**Definition of Done:**

- [ ] The catalog contains exact current state, path, plugin, backend, deployment, and lifecycle vocabularies.
- [ ] `ABANDONED` and row union are explicit.
- [ ] Duplicate-key and false-completion manifests fail deterministically.
- [ ] One validator implementation owns both local and CI package checks.
- [ ] v1 remains immutable historical evidence.

---

### Task 3: Publish a current full pinning assessment and live work tree

**Files:**

- Create: `docs/architecture/state_engine/assessments/<timestamp>/README.md`
- Create: `docs/architecture/state_engine/assessments/<timestamp>/assessment.json`
- Create: `docs/architecture/state_engine/assessments/<timestamp>/evidence.md`
- Create: `docs/architecture/state_engine/assessments/<timestamp>/review.md`
- Modify: `docs/architecture/state_engine/architecture.md`
- Modify: `docs/architecture/state_engine/proof-matrix.md`
- Modify: `docs/architecture/state_engine/README.md`
- Modify: `docs/architecture/state_engine/assessments/README.md`

**Step 1: Freeze a clean assessment baseline**

Create a dedicated assessment worktree at the exact candidate commit, run `uv sync --frozen --all-extras`, and capture every Git/environment field required by `assessment-program.md`.

Verify:

```bash
.venv/bin/python -c 'import elspeth; print(elspeth.__file__)'
git status --porcelain=v2 --branch --untracked-files=all
```

Expected: the import is inside the assessment worktree and there is no behavioral overlay.

**Step 2: Refresh structural and temporal authorities**

- Require Loomweave `project_status_get.staleness == "fresh"` at the assessment commit.
- Capture a fresh full Warpline edge snapshot at the same commit.
- Run `changed` and `reverify` from the prior assessment baseline to the new baseline.
- Capture exact, non-truncated Filigree JSON for `[state engine]`, ready, and blocked work.

If the edge snapshot remains partial or stale, record that limitation and do not make a downstream unreachability claim.

**Step 3: Initialize the v2 full package**

Run:

```bash
STATE_ASSESSMENT_ID="$(TZ=Australia/Canberra date '+%Y-%m-%d-%H%M')"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  init-full "$STATE_ASSESSMENT_ID" \
  "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID"
```

Expected: a full manifest containing every v2 leg, cell, and hard gate with no implicit passes.

**Step 4: Re-execute reusable current evidence**

Do not inherit any 2026-07-18 pass merely because its test still exists. Collect and run exact current selectors, beginning with production paths and then direct repository tests. Attach each result only to assertions it actually proves.

**Step 5: Reconcile Filigree ownership**

Retain the six existing open issues where their exit gates still match current evidence. Create coherent cohort owners for newly visible gaps:

- support-profile/catalog drift;
- read-model truth tables;
- run coordination and independent-process recovery;
- barrier family, including row union;
- sink-effect lifecycle and external publication;
- `ABANDONED`/run-finalization behavior;
- first-party plugin lifecycle coverage;
- final assessment and maintained gates.

Create one issue per coherent implementation/evidence cohort, never one per cell. Wire dependencies so implementation cohorts depend on the pinning assessment and final completion depends on every cohort.

Create the hierarchy with `filigree create-plan`. Write this JSON to a
temporary file, replacing only titles if the pinning assessment discovers a
materially different cohort boundary:

```json
{
  "milestone": {
    "title": "State Engine v2 pinning and completion",
    "priority": 1
  },
  "phases": [
    {
      "title": "Pin the current contract",
      "steps": [
        {"title": "Reconcile supported profiles and publish catalog v2", "priority": 1},
        {"title": "Publish the current full pinning assessment", "priority": 1, "deps": [0]}
      ]
    },
    {
      "title": "Close implementation and proof cohorts",
      "steps": [
        {"title": "Close queue source transform and gate contracts", "priority": 1, "deps": ["0.1"]},
        {"title": "Close lease coordination process and read-model contracts", "priority": 1, "deps": ["0.1"]},
        {"title": "Close aggregation coalesce and row-union contracts", "priority": 1, "deps": ["0.1"]},
        {"title": "Close sink-effect publication and repair contracts", "priority": 1, "deps": ["0.1"]},
        {"title": "Close lifecycle abandonment follower and plugin contracts", "priority": 1, "deps": ["0.1"]}
      ]
    },
    {
      "title": "Publish completion",
      "steps": [
        {
          "title": "Run the final full assessment and install maintained gates",
          "priority": 1,
          "deps": ["1.0", "1.1", "1.2", "1.3", "1.4"]
        }
      ]
    }
  ]
}
```

Run:

```bash
filigree create-plan --file /tmp/state-engine-v2-plan.json
```

Then link the six pre-existing open issues as context or dependencies rather
than creating duplicates. Do not claim implementation issues during planning.

**Step 6: Validate and commit the pinning package**

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-package "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID/assessment.json"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py check-links
git diff --check
```

Expected: a valid current package. Its verdict may still be `not_complete`; this task pins truth, it does not manufacture completion.

Commit:

```bash
git add docs/architecture/state_engine
git commit -m "docs(state-engine): publish the current v2 pinning assessment"
```

**Definition of Done:**

- [ ] The hub points at the current baseline and v2 catalog.
- [ ] Every cell has current status, reason, exit gate, and owner field.
- [ ] Closed historical issues are not reused as owners for residual work.
- [ ] Loomweave, Warpline, and Filigree limitations are explicit.
- [ ] The package validator passes without editing historical assessments.

---

### Task 4: Build one durable-image assertion and process-crash harness

**Files:**

- Create: `tests/helpers/state_engine.py`
- Modify: `tests/e2e/recovery/harness.py`
- Create: `tests/unit/helpers/test_state_engine_image.py`
- Create: `tests/e2e/recovery/test_state_engine_process_harness.py`

**Step 1: Write failing durable-image tests**

The helper must capture canonical rows for all state/evidence planes relevant to a run and support exact equality plus an allowlisted delta:

```python
def test_durable_image_reports_only_allowlisted_delta(factory, seeded_run) -> None:
    before = capture_state_engine_image(factory, run_id=seeded_run.run_id)
    factory.scheduler.heartbeat_lease(
        run_id=seeded_run.run_id,
        work_item_id=seeded_run.work_item_id,
        lease_owner=seeded_run.worker_id,
        lease_seconds=60,
        now=seeded_run.now,
        membership_fenced=True,
    )
    after = capture_state_engine_image(factory, run_id=seeded_run.run_id)

    assert before.diff(after).changed_tables == {"token_work_items"}
    assert before.diff(after).changed_columns == {"token_work_items": {"lease_expires_at"}}
```

**Step 2: Verify RED**

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/helpers/test_state_engine_image.py \
  tests/e2e/recovery/test_state_engine_process_harness.py
```

Expected: import/fixture failures because the helpers do not exist.

**Step 3: Implement the durable image**

Capture, when present:

- runs and source lifecycle;
- rows, tokens, and token outcomes;
- token work items and scheduler events;
- node states, routing events, branch losses, batches, and barrier arrivals;
- run coordination, worker membership, and coordination events;
- checkpoints;
- sink effects, members, attempts, calls, artifacts, and audit-export snapshots.

Normalize mappings by explicit column name, datetimes to ISO-8601, enums to values, and binary values to hashes. Never use `getattr`; SQLAlchemy row columns must be read through `row._mapping`.

**Step 4: Implement abrupt-process control**

Extend the E2E harness with a spawn-based child protocol that can:

- open its own database connection;
- block on a named seam after signalling readiness;
- be terminated with `SIGKILL`/`Process.kill()`;
- leave the parent a bounded readiness/exit oracle;
- reopen with fresh production objects and call public `resume()` or `join`.

Threads may coordinate deterministic direct-repository lock tests, but they cannot satisfy process-death or process-scoped contention cells.

**Step 5: Run GREEN tests**

Run the same command from Step 2. Expected: all tests pass without leaked child processes or timeout hangs.

**Step 6: Commit**

```bash
git add tests/helpers/state_engine.py \
  tests/e2e/recovery/harness.py \
  tests/unit/helpers/test_state_engine_image.py \
  tests/e2e/recovery/test_state_engine_process_harness.py
git commit -m "test(state-engine): add durable image and process crash harness"
```

**Definition of Done:**

- [ ] Zero-mutation tests compare every applicable durable plane.
- [ ] Abrupt-process tests use separate operating-system processes and connections.
- [ ] Every wait is bounded and cleans up children on failure.
- [ ] Helpers run against SQLite and can accept an explicit PostgreSQL URL.

---

### Task 5: Close queue, source, transform, and gate transition cohorts

**Files:**

- Modify: `tests/unit/core/landscape/test_scheduler_events.py`
- Modify: `tests/unit/core/landscape/test_scheduler_fencing.py`
- Create: `tests/unit/core/landscape/test_scheduler_queue_contract.py`
- Modify: `tests/e2e/recovery/test_concurrent_resume.py`
- Create: `tests/e2e/recovery/test_source_ingress_contract.py`
- Create: `tests/integration/pipeline/test_scheduler_plugin_dispositions.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler/queue.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler/dispositions.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler_repository.py`
- Modify as defects require: `src/elspeth/engine/scheduler_drain.py`
- Modify as defects require: `src/elspeth/engine/processor.py`
- Modify as defects require: `src/elspeth/engine/executors/transform.py`
- Modify as defects require: `src/elspeth/engine/executors/gate.py`

**Step 1: Add RED cases for TS-00/01/02 and F-11**

Cover exact replay, incompatible replay, enqueue-plus-event rollback, row/token/source/lease composition, yielded-row failure, pre-row load/iterator failure, and quarantine exclusion. Every refusal must compare a complete durable image.

**Step 2: Run the focused queue/source tests**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_scheduler_queue_contract.py \
  tests/e2e/recovery/test_source_ingress_contract.py \
  tests/e2e/recovery/test_concurrent_resume.py
```

Expected: new assertions fail for an observed missing contract only. If a case passes immediately, record it as evidence rather than changing production code.

**Step 3: Add RED production plugin dispositions**

Run one real failing transform and one real declarative gate through the scheduler drain. Assert node state, scheduler status/event, payload scrub, branch loss, route, owner/fence, and token outcome together.

**Step 4: Implement only demonstrated defects**

Fix at the repository or production-boundary owner. Do not add a second compatibility path, weaken ownership, or make a production function tolerate a malformed test fake.

**Step 5: Run GREEN cohorts and current adjacent tests**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_scheduler_queue_contract.py \
  tests/unit/core/landscape/test_scheduler_events.py \
  tests/unit/core/landscape/test_scheduler_fencing.py \
  tests/e2e/recovery/test_source_ingress_contract.py \
  tests/e2e/recovery/test_concurrent_resume.py \
  tests/integration/pipeline/test_scheduler_plugin_dispositions.py
```

**Step 6: Update evidence and close exact owners**

Create a new delta assessment and update only cells proved by the selectors.
Close `elspeth-c0d4a28e11`, `elspeth-9cd07962c7`, and
`elspeth-2e66723070` only when their descriptions and assessment exit gates
are fully satisfied. Never edit the Task 3 full package.

**Step 7: Commit**

```bash
git add src/elspeth/core/landscape/scheduler \
  src/elspeth/core/landscape/scheduler_repository.py \
  src/elspeth/engine/scheduler_drain.py \
  src/elspeth/engine/processor.py \
  src/elspeth/engine/executors \
  tests/unit/core/landscape \
  tests/e2e/recovery \
  tests/integration/pipeline \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): close queue and plugin disposition contracts"
```

**Definition of Done:**

- [ ] TS-00/01/02/07/08/09/10, PB-01/02/03, and F-11 cells are promoted only where all required dimensions pass.
- [ ] State and mandatory evidence rollback together.
- [ ] Production plugin calls, not helper-only calls, own boundary evidence.
- [ ] Existing issue closure matches current assessment cells.

---

### Task 6: Close leases, run coordination, and independent-process authority

**Files:**

- Modify: `tests/unit/core/landscape/test_scheduler_lease_recovery_races.py`
- Modify: `tests/unit/core/landscape/test_run_coordination_repository.py`
- Modify: `tests/testcontainer/core/test_scheduler_lease_eviction_postgres.py`
- Modify: `tests/testcontainer/core/test_run_coordination_release_postgres.py`
- Create: `tests/e2e/recovery/test_registered_process_authority.py`
- Modify: `tests/e2e/recovery/test_follower_coordination_chaos.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler/leases.py`
- Modify as defects require: `src/elspeth/core/landscape/run_coordination_repository.py`
- Modify as defects require: `src/elspeth/engine/scheduler_drain.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/heartbeat.py`

**Step 1: Add the complete lease truth table**

For READY, transform LEASED, sink-redrive LEASED, PENDING_SINK, expired equality, foreign run, foreign owner, inactive member, stale epoch, and stall-budget arms, assert exact winner, loser, generation/attempt, bundle, events, and zero-mutation refusal.

**Step 2: Add registered OS-process scenarios**

Use the Task 4 harness to prove:

- one READY claimant;
- one PENDING_SINK claimant;
- expired transform and sink-redrive takeover;
- active heartbeat prevents takeover;
- heartbeat CAS loss causes production result abandonment and no disposition;
- leader seat register/takeover/release and follower admit/depart/evict;
- losing/deposed processes cannot mutate after the winner.

**Step 3: Add PostgreSQL transaction cases**

Use PostgreSQL 16 and independent connections/processes for conditional updates, row locks, exact expiry, and concurrent release/eviction. Keep multi-replica product support out of the assertion text.

**Step 4: Run RED**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_scheduler_lease_recovery_races.py \
  tests/unit/core/landscape/test_run_coordination_repository.py \
  tests/e2e/recovery/test_registered_process_authority.py \
  tests/e2e/recovery/test_follower_coordination_chaos.py
```

Run PostgreSQL separately:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/core/test_scheduler_lease_eviction_postgres.py \
  tests/testcontainer/core/test_run_coordination_release_postgres.py
```

**Step 5: Implement minimal fixes and rerun GREEN**

Fix only failing owner checks, transactional composition, expiry boundaries, or production reactions. Preserve conditional-update fencing; never replace it with a check-then-write sequence.

**Step 6: Update evidence, issues, and commit**

Close `elspeth-9a52eb80f9` and `elspeth-2aba594afb` only after production process evidence passes.

```bash
git add src/elspeth/core/landscape/scheduler/leases.py \
  src/elspeth/core/landscape/run_coordination_repository.py \
  src/elspeth/engine/scheduler_drain.py \
  src/elspeth/engine/orchestrator/heartbeat.py \
  tests/unit/core/landscape \
  tests/e2e/recovery \
  tests/testcontainer/core \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): prove process authority and lease recovery"
```

**Definition of Done:**

- [ ] TS-03–06, AUX-01/02/06/07, and RC-01–07 have bounded independent-process evidence.
- [ ] PostgreSQL and SQLite cases remain separately attributable.
- [ ] Sink-redrive bundles survive claim, expiry, recovery, and refusal exactly.
- [ ] A losing process has a deterministic terminal observation and cannot disposition work.

---

### Task 7: Complete all orchestration read-model and forbidden-path truth tables

**Files:**

- Create: `tests/unit/core/landscape/test_state_engine_read_model_truth_tables.py`
- Create: `tests/integration/pipeline/test_state_engine_read_model_consumers.py`
- Create: `tests/unit/core/landscape/test_state_engine_forbidden_paths.py`
- Create: `tests/testcontainer/core/test_state_engine_read_model_truth_tables_postgres.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler/restore_read_model.py`
- Modify as defects require: `src/elspeth/core/landscape/run_status_projection.py`
- Modify as defects require: `src/elspeth/core/landscape/run_coordination_repository.py`
- Modify as defects require: `src/elspeth/core/checkpoint/recovery.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/resume.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/run_status.py`
- Modify as defects require: `src/elspeth/web/execution/accounting.py`

**Step 1: Express each RM leg as data**

For RM-01 through RM-14, define a table of seeded rows, expected repository result, and expected production decision. Include positive and negative status/subtype arms, foreign run/owner, exact expiry equality, duplicates, ordering, malformed identities, and backend profile. RM-14 must cover recovery worksets, terminal-status projection, and accounting census rather than treating `run_status.py` as the whole read model.

Use direct function objects in parametrization; do not resolve functions by dynamic attribute name.

**Step 2: Add forbidden-path cases**

Own F-04, F-06, F-07, F-10, and F-12 in this task, matching the pinned proof-matrix cohort. Assert the exact exception/error category and complete before/after durable image. Reach every production surface that could attempt the path; where a path is deliberately absent, attach fresh Loomweave caller evidence and an executable architecture gate.

Integrate the retained evidence for the other forbidden legs from their assigned cohorts (Tasks 5, 8, 9, and 10/11); do not reassign, duplicate, or close those owners from Task 7. F-10 permits only its contracted best-effort `fence_refusal` event as an explicit image delta.
F-07's valid-leader zero-recovery arm is a fence-as-heartbeat operation under
ADR-030, so it permits only the exact leader `heartbeat_at` and
`heartbeat_expires_at` extension made by the fenced transaction; scheduler
rows, attempts, leases, outcomes, and scheduler/audit events must remain
byte-for-byte unchanged. Stale or foreign F-07 refusals remain fully
zero-mutation.

**Step 3: Run RED**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_state_engine_read_model_truth_tables.py \
  tests/integration/pipeline/test_state_engine_read_model_consumers.py \
  tests/unit/core/landscape/test_state_engine_forbidden_paths.py
```

Run PostgreSQL 16 separately as reporter-free backend-support qualification:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/core/test_state_engine_read_model_truth_tables_postgres.py
```

`observe_postgresql` denotes the actual maintained AWS composition, so a local
testcontainer must not call it or emit an AWS profile report. Retain this result
as backend-support evidence only. The AWS PostgreSQL 16 read-model cells remain
`unknown` until the real protected AWS lane exercises the production
composition.

Repository-only selectors and mocked consumer decisions remain separately attributable; neither is evidence of their production composition by itself. Likewise, one generic SQLite repository run must not be relabelled as all three SQLite deployment profiles.

**Step 4: Fix selectors at their owners**

Do not compensate for a faulty read model in an orchestration consumer. Repair the repository selector, then prove the production consumer uses it and acts on its exact result.

**Step 5: Run GREEN and commit**

```bash
git add src/elspeth/core/landscape/scheduler/restore_read_model.py \
  src/elspeth/core/landscape/run_status_projection.py \
  src/elspeth/core/landscape/run_coordination_repository.py \
  src/elspeth/core/checkpoint/recovery.py \
  src/elspeth/engine/orchestrator \
  src/elspeth/web/execution/accounting.py \
  tests/unit/core/landscape/test_state_engine_read_model_truth_tables.py \
  tests/integration/pipeline/test_state_engine_read_model_consumers.py \
  tests/unit/core/landscape/test_state_engine_forbidden_paths.py \
  tests/testcontainer/core/test_state_engine_read_model_truth_tables_postgres.py \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): close read-model and refusal matrices"
```

**Definition of Done:**

- [ ] RM-01–14 truth tables cover every current state/subtype arm.
- [ ] Each read model has a production consumer assertion.
- [ ] F-04/06/07/10/12 refusal includes complete zero-mutation evidence, with
  only the contracted F-10 event delta and F-07 valid-leader heartbeat delta
  described above.
- [ ] Other F-leg evidence remains attached to its pinned cohort owner and is integrated without duplicate closure.
- [ ] SQLite and PostgreSQL 16 evidence is separately attributable, and deployment-profile labels match exercised boundaries.
- [ ] No unproved read-model arm controls drain, flush, resume, eviction, or completion.

---

### Task 8: Complete aggregation, coalesce, and row-union barrier recovery

**Files:**

- Modify: `tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py`
- Modify: `tests/integration/pipeline/test_aggregation_recovery.py`
- Create: `tests/integration/pipeline/test_coalesce_process_recovery.py`
- Modify: `tests/integration/pipeline/test_row_union_ab_experiment.py`
- Modify: `tests/integration/pipeline/test_row_union_branch_loss.py`
- Create: `tests/e2e/recovery/test_barrier_process_death_matrix.py`
- Create: `tests/testcontainer/core/test_barrier_recovery_postgres.py`
- Modify as defects require: `src/elspeth/core/landscape/scheduler/barrier.py`
- Modify as defects require: `src/elspeth/engine/barrier_coordination.py`
- Modify as defects require: `src/elspeth/engine/coalesce_executor.py`
- Modify as defects require: `src/elspeth/engine/executors/aggregation.py`
- Modify as defects require: `src/elspeth/engine/row_union_executor.py`
- Modify as defects require: `src/elspeth/engine/processor.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/aggregation.py`
- Modify as contract changes require: `docs/architecture/state_engine/architecture.md`

**Step 1: Define seam cases before implementation**

For aggregation, coalesce, and row union, kill the process:

- before BLOCKED adoption;
- after adoption but before plugin/effect execution;
- after result/effect persistence but before barrier completion;
- after exact snapshot consumption but before continuation observation;
- after READY/PENDING_SINK emission but before downstream execution;
- after branch loss or timeout but before terminal publication.

**Step 2: Assert exact group identity**

Pin member identities, branch loss reason, release order, parent/child tokens, result/effect identity, scheduler events, outcome rows, and downstream artifact count. Compare recovered runs to uninterrupted semantic controls, allowing only named recovery-claim provenance differences.

In particular, make the aggregation image with a committed batch/result and terminal input outcomes, but BLOCKED scheduler rows and no continuation, executable. Recovery must reconstruct the continuation without replaying the committed aggregation transform or effect.

**Step 3: Run RED**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py \
  tests/integration/pipeline/test_aggregation_recovery.py \
  tests/integration/pipeline/test_coalesce_process_recovery.py \
  tests/integration/pipeline/test_row_union_ab_experiment.py \
  tests/integration/pipeline/test_row_union_branch_loss.py \
  tests/e2e/recovery/test_barrier_process_death_matrix.py
```

Run PostgreSQL 16 separately as reporter-free backend-support qualification;
these assertions cover local PostgreSQL transaction semantics only and do not
claim the AWS deployment composition or multi-replica scheduling:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/core/test_barrier_recovery_postgres.py
```

Do not call `observe_postgresql` for this local testcontainer run. The AWS
PostgreSQL 16 barrier cells remain `unknown` until the protected live lane
executes the real maintained AWS composition.

Define one genuinely exercised process-matrix selector for each SQLite deployment profile and retain them in separate reporter sessions:

```bash
for deployment in \
  single_process_leader \
  same_host_leader_plus_claim_only_followers \
  web_hosted_leader_plus_same_host_cli_followers
do
  PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
    -p scripts.state_engine_profile_reporter \
    --state-engine-profile-report="<assessment>/evidence/task8-${deployment}.profile.json" \
    tests/e2e/recovery/test_barrier_process_death_matrix.py \
    -k "$deployment"
done
```

The named selector must call `observe_sqlite` with the matching live deployment boundary; relabelling one generic SQLite run does not satisfy this step.

**Step 4: Fix only demonstrated continuation gaps**

All state/evidence needed after a crash must commit in the barrier completion transaction or be named by a durable continuation that a fresh leader deterministically claims. Never repair by replaying plugin work whose committed result/effect already exists.

**Step 5: Run GREEN and commit**

```bash
git add src/elspeth/core/landscape/scheduler/barrier.py \
  src/elspeth/engine/barrier_coordination.py \
  src/elspeth/engine/coalesce_executor.py \
  src/elspeth/engine/executors/aggregation.py \
  src/elspeth/engine/row_union_executor.py \
  src/elspeth/engine/processor.py \
  src/elspeth/engine/orchestrator/aggregation.py \
  tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py \
  tests/integration/pipeline \
  tests/e2e/recovery/test_barrier_process_death_matrix.py \
  tests/testcontainer/core/test_barrier_recovery_postgres.py \
  docs/architecture/state_engine/architecture.md \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): complete durable barrier recovery"
```

**Definition of Done:**

- [ ] TS-15–18 pass for every applicable barrier family; Task 5's TS-07 BLOCKED-entry evidence is integrated without duplicate ownership.
- [ ] AUX-03–05 and PB-04/05/10 pass across restart seams.
- [ ] Row union loss, timeout, late arrival, and recovery cases are explicit.
- [ ] PostgreSQL 16 and each SQLite deployment profile are separately attributable to the boundary actually exercised.
- [ ] No group releases twice, no committed continuation is lost, and committed aggregation work is never replayed to reconstruct one.

---

### Task 9: Complete sink-effect and post-publication recovery

**Files:**

- Modify: `tests/integration/pipeline/test_sink_effect_recovery.py`
- Modify: `tests/integration/pipeline/test_sink_effect_lease_wait_recovery.py`
- Modify: `tests/integration/pipeline/test_builtin_sink_effect_recovery.py`
- Modify: `tests/integration/pipeline/test_audit_export_effect_recovery.py`
- Create: `tests/integration/pipeline/test_sink_effect_recovery_aws_live.py`
- Modify: `tests/unit/core/landscape/test_scheduler_events.py`
- Modify: `tests/unit/plugins/sinks/test_json_sink.py`
- Modify: `tests/e2e/recovery/test_suspended_winner_fences.py`
- Modify: `tests/testcontainer/core/test_sink_effect_lock_order_postgres.py`
- Create: `tests/e2e/recovery/test_sink_effect_process_death_matrix.py`
- Modify as defects require: `src/elspeth/core/landscape/execution/sink_effect_reservation.py`
- Modify as defects require: `src/elspeth/core/landscape/execution/sink_effect_lifecycle.py`
- Modify as defects require: `src/elspeth/core/landscape/execution/sink_effect_finalization.py`
- Modify as defects require: `src/elspeth/engine/executors/sink_effects.py`
- Modify as defects require: `src/elspeth/plugins/sinks/aws_s3_sink.py`
- Modify as defects require: `src/elspeth/plugins/sinks/json_sink.py`
- Modify as contract changes require: `docs/architecture/state_engine/architecture.md`

**Step 1: Materialize every PB-06 and PB-07 case**

Use the named cases already present in v1, plus backend/profile cases introduced in v2. Include `reserve-audit-export-snapshot`, the TS-11–14/F-08 scheduler-event arms, and both sink-specific stale-winner fences. No broad “sink recovery passed” selector may stand in for omitted cases.

**Step 2: Add abrupt-process seams**

Kill and resume before/after reservation, preparation claim, inspection, plan CAS, publication, response, finalization, and scheduler callback. Assert one effect ID, generation discipline, one effective external publication, exact attempt evidence, one artifact, terminal outcomes, and TS-14 repair without republishing.

Capture both the complete Landscape image and the external target image at every seam. A deterministic effect ID identifies one logical effect; it does not by itself prove one physical provider request. Prove one effective target state/version/ledger winner with the adapter's conditional-write or reconciliation contract, while accounting for failed or response-lost attempts. When the target cannot establish the exact result, the correct verdict is `UNKNOWN` and publication remains blocked.

**Step 3: Exercise both stores**

- SQLite: production pipeline with local deterministic sinks and OS-process death.
- Local PostgreSQL 16 testcontainer: lock order, concurrent reservation/finalization/takeover, returned-result finalization, and ADR-038 token/outcome races. This proves backend transaction semantics, not the AWS deployment boundary by itself.
- Maintained AWS profile: an AWS-backed production sink-effect coordinator matrix against a real PostgreSQL 16 Landscape connection and a real supported external sink (initially conditional S3), under the single-leader topology. Execute every applicable PB-06 named case and PB-07 process-loss seam, plus their TS-11–14/F-08 scheduler outcomes. Missing credentials or an unavailable environment leaves the profile cells `unknown`; it is never relabelled from the local testcontainer run.

**Step 4: Run RED**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/integration/pipeline/test_sink_effect_recovery.py \
  tests/integration/pipeline/test_sink_effect_lease_wait_recovery.py \
  tests/integration/pipeline/test_builtin_sink_effect_recovery.py \
  tests/integration/pipeline/test_audit_export_effect_recovery.py \
  tests/e2e/recovery/test_sink_effect_process_death_matrix.py
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/core/test_sink_effect_lock_order_postgres.py
```

After those cohorts stabilize, discover three genuinely distinct SQLite deployment selections and the live AWS PostgreSQL/S3 coordinator matrix in separate pytest processes. Materialize each selection as exact node IDs and capture it under the retained pytest evidence rule above; the file/`-k` commands below are selection sketches only. The live AWS tests must be marked `live_aws`, must call `observe_postgresql` on the real Landscape connection, and must fail the evidence gate when skipped or uncollected:

```bash
for deployment in \
  single_process_leader \
  same_host_leader_plus_claim_only_followers \
  web_hosted_leader_plus_same_host_cli_followers
do
  PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
    -p scripts.state_engine_profile_reporter \
    --state-engine-profile-report="<assessment>/evidence/task9-${deployment}.profile.json" \
    tests/e2e/recovery/test_sink_effect_process_death_matrix.py \
    -k "$deployment"
done

PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  -m "integration and live_aws" \
  -p scripts.state_engine_profile_reporter \
  --state-engine-profile-report=<assessment>/evidence/task9-aws-postgresql.profile.json \
  tests/integration/pipeline/test_sink_effect_recovery_aws_live.py
```

**Step 5: Fix defects at the effect boundary**

Preserve the closed reconciliation vocabulary. `UNKNOWN` must stop publication; `APPLIED_WITH_EXACT_DESCRIPTOR` must finalize without another commit; `NOT_APPLIED` may commit once. Every takeover/finalization write remains generation fenced.

Resolve Task 9's half of `elspeth-800d00c03e` without inventing an unfenced operation lease: for an effect-linked operation, `open` means the logical operation is unfinished and may be actively executing or interrupted; the associated sink effect's lease and generation are the only current-custody authority. Recovery reuses the same open operation/effect and records interruption/resumption in effect attempts and generations. Atomic effect finalization writes `completed`. Do not repurpose the existing completion-shaped `pending` operation status or reopen terminal rows. Add RED cases for abrupt death with an open operation, generation-fenced recovery to completion, and absence of any second operation/effect. Document why this operation lifecycle is distinct from ADR-019's retained token-outcome axes.

Task 10 owns the other half: only fenced non-resumable run finalization may move an existing effect-linked operation from `open` to `failed`, atomically with the run stamp and ABANDONED outcomes; resume is prohibited afterward. Ordinary pre-reservation sink, source, and runtime operation failures keep their existing writers. Keep the Task 9 owner open until that Task 10 evidence is linked rather than duplicating run-finalization ownership here.

Resolve `elspeth-e08643b495` by validating JSON encoding before accepting the member and diverting unrepresentable rows with the same attributed category as sibling file sinks. Refresh `JSONSink.source_file_hash` mechanically **after** formatting, using `scripts/cicd/plugin_hash.py`, and run the exact plugin-hash/family gate. Do not close Task 9 while either demonstrated lifecycle defect remains unresolved.

**Step 6: Run GREEN and commit**

```bash
git add src/elspeth/core/landscape/execution/sink_effect_reservation.py \
  src/elspeth/core/landscape/execution/sink_effect_lifecycle.py \
  src/elspeth/core/landscape/execution/sink_effect_finalization.py \
  src/elspeth/engine/executors/sink_effects.py \
  src/elspeth/plugins/sinks/aws_s3_sink.py \
  src/elspeth/plugins/sinks/json_sink.py \
  docs/architecture/state_engine/architecture.md \
  tests/integration/pipeline/test_sink_effect_recovery.py \
  tests/integration/pipeline/test_sink_effect_lease_wait_recovery.py \
  tests/integration/pipeline/test_builtin_sink_effect_recovery.py \
  tests/integration/pipeline/test_audit_export_effect_recovery.py \
  tests/integration/pipeline/test_sink_effect_recovery_aws_live.py \
  tests/unit/core/landscape/test_scheduler_events.py \
  tests/unit/plugins/sinks/test_json_sink.py \
  tests/e2e/recovery/test_suspended_winner_fences.py \
  tests/e2e/recovery/test_sink_effect_process_death_matrix.py \
  tests/testcontainer/core/test_sink_effect_lock_order_postgres.py \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): complete sink-effect recovery proofs"
```

**Definition of Done:**

- [ ] TS-11–14, PB-06/PB-07, and F-08 named cases are resolved for every required profile.
- [ ] External visibility never exceeds one effective target state/version/ledger winner; request-attempt counts are reported separately.
- [ ] Finalization and scheduler callback loss converge without republishing.
- [ ] Recoverable interruption keeps the same unfinished effect-linked `open` operation/effect and custody comes only from the effect lease/generation.
- [ ] Task 10's fenced non-resumable finalization evidence is linked before the Task 9 owner closes.
- [ ] Local PostgreSQL 16 semantics and the live AWS PostgreSQL/S3 production boundary are separately attributable.
- [ ] Legacy `write`/`flush` remains unreachable as a production publication path.

---

### Task 10: Complete run finalization, `ABANDONED`, follower, and lifecycle behavior

**Files:**

- Modify: `tests/unit/core/landscape/test_run_finalization_abandonment.py`
- Modify: `tests/testcontainer/core/test_token_outcome_atomicity_postgres.py`
- Modify: `tests/e2e/recovery/test_follower_join_and_drain.py`
- Modify: `tests/e2e/recovery/test_run_coordination_uniformity.py`
- Create: `tests/e2e/recovery/test_run_lifecycle_process_matrix.py`
- Create: `tests/e2e/recovery/test_run_lifecycle_aws_live.py`
- Create: `tests/integration/cli/test_state_engine_lifecycle_profiles.py`
- Modify: `tests/unit/web/execution/test_run_accounting_projection.py`
- Modify: `tests/unit/engine/orchestrator/test_terminal_pair_counter_parity.py`
- Modify as defects require: `src/elspeth/core/landscape/run_lifecycle_repository.py`
- Modify as defects require: `src/elspeth/core/landscape/execution/operations.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/run_lifecycle.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/follower.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/resume.py`
- Modify as defects require: `src/elspeth/engine/orchestrator/run_status.py`
- Modify as defects require: `src/elspeth/web/execution/accounting.py`
- Modify as defects require: `src/elspeth/cli.py`
- Modify as contract changes require: `docs/architecture/adr/038-non-terminal-abandoned-path.md`
- Modify as contract changes require: `docs/architecture/state_engine/architecture.md`

**Step 1: Add the complete ADR-038 matrix**

Prove:

- fenced FAILED/INTERRUPTED plus non-resumable source state records one ABANDONED row per undecided token in the same transaction;
- resumable runs remain pending, never abandoned;
- unfenced orphan finalization under-abandons as documented;
- repeat completion is idempotent;
- any sweep failure rolls back the run stamp;
- concurrent decided-outcome versus abandonment is serialized on PostgreSQL;
- decided-plus-abandoned images fail closed in accounting and resume;
- ABANDONED never reaches processing counters.
- fenced non-resumable finalization atomically moves every existing effect-linked `open` operation to `failed` with the run stamp and ABANDONED outcomes, while resumable runs leave those operations open and terminal effect operations remain unchanged;
- resume refuses the non-resumable failed-operation image and no path reopens a terminal operation.

Compute the fenced non-resumability decision once, then run independent token and effect-operation sweeps inside the same terminal transaction. Do not place the operation sweep beneath `_abandon_undecided_tokens_in()`'s no-undecided-token early return. A failed effect-linked operation must receive `completed_at`, non-null `duration_ms`, and a bounded non-sensitive `error_message`; any sweep failure rolls back the run stamp, ABANDONED rows, and operation failures together.

Implement one operation-transition primitive in `execution/operations.py` that accepts the caller-owned transaction and enforces the `open -> failed` invariants. `run_lifecycle_repository.py` composes that primitive inside its fenced terminal transaction; it must not duplicate the invariant with a raw `UPDATE`.

**Step 2: Exercise real follower traversal**

Build the follower through the real CLI/production assembly (`_build_resume_graphs`, plugin context/start/cleanup, `build_follower_processor`, `FollowerProcessor`, and `RowProcessor` traversal), run real transform and gate work, and assert terminal, blocked, lossy, sink-handoff, transform-failure, heartbeat-loss, and teardown behavior. Repository dispositions or stubbed drain loops are supporting evidence only. Revalidate the incomplete follower `PluginContext` tracked by `elspeth-6b6a62af1f`; do not close PB-08 if a supported plugin cannot traverse the real context.

**Step 3: Cover lifecycle modes**

Run fresh, resume, follower, partial-start failure, normal teardown, exceptional teardown, and leader takeover with exact worker/seat/checkpoint/run/outcome images.

Retain three separately observed SQLite deployment selections. The web-hosted profile must exercise the actual CLI-hosted composition, not relabel a generic follower. Run PostgreSQL transaction/race cases locally on PostgreSQL 16, but keep them separate from the live AWS single-leader lifecycle matrix against the real PostgreSQL 16 Landscape connection. Local testcontainers cannot promote the AWS boundary and none of these cases implies multi-replica support.

**Step 4: Run RED**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/core/landscape/test_run_finalization_abandonment.py \
  tests/e2e/recovery/test_follower_join_and_drain.py \
  tests/e2e/recovery/test_run_coordination_uniformity.py \
  tests/e2e/recovery/test_run_lifecycle_process_matrix.py \
  tests/integration/cli/test_state_engine_lifecycle_profiles.py
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 -m testcontainer \
  tests/testcontainer/core/test_token_outcome_atomicity_postgres.py
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  -m "integration and live_aws" \
  tests/e2e/recovery/test_run_lifecycle_aws_live.py
```

After RED/GREEN stabilizes, materialize exact node lists under the retained pytest evidence rule for:

- `single-process-leader` from the lifecycle process matrix;
- `same-host-leader-plus-claim-only-followers` from real follower traversal;
- `web-hosted-leader-plus-same-host-cli-followers` from the CLI lifecycle profile test;
- `aws-single-leader-landscape` from the `live_aws` lifecycle matrix using the real PostgreSQL 16 Landscape connection.

Retain a fifth, support-only PostgreSQL 16 testcontainer record for the exact transaction/lock/race nodes. Attribute it only to backend-semantic concurrency/atomicity support; it cannot replace the AWS deployment-boundary record.

Each selection runs in its own reporter/JUnit process. The AWS matrix must prove the maintained AWS composition and safe deployment provenance, not merely a local pytest process pointed at a remote PostgreSQL URL. Skipped or unavailable live AWS evidence remains `unknown`.

**Step 5: Implement minimal fixes, rerun GREEN, and close exact owners**

Close `elspeth-6f6bbbec00` only after the real `FollowerProcessor` path is evidenced.
Claim `elspeth-800d00c03e` as the narrow cross-cohort operation-finalization issue for this task. Make the Task 9 sink owner depend on that issue; do **not** make Task 10 or the lifecycle cohort depend on Task 9. Close `elspeth-800d00c03e` after the Task 10 atomic finalization evidence lands, then notify the Task 9 owner to re-evaluate its remaining exits. Keep `elspeth-67be892457` open while the Python 3.14 blocker, Task 11 plugin cases, or any required profile remains unresolved.

**Step 6: Commit**

```bash
git add src/elspeth/core/landscape/run_lifecycle_repository.py \
  src/elspeth/core/landscape/execution/operations.py \
  src/elspeth/engine/orchestrator/run_lifecycle.py \
  src/elspeth/engine/orchestrator/follower.py \
  src/elspeth/engine/orchestrator/resume.py \
  src/elspeth/engine/orchestrator/run_status.py \
  src/elspeth/web/execution/accounting.py \
  src/elspeth/cli.py \
  docs/architecture/adr/038-non-terminal-abandoned-path.md \
  docs/architecture/state_engine/architecture.md \
  tests/unit/core/landscape/test_run_finalization_abandonment.py \
  tests/unit/web/execution/test_run_accounting_projection.py \
  tests/unit/engine/orchestrator/test_terminal_pair_counter_parity.py \
  tests/testcontainer/core/test_token_outcome_atomicity_postgres.py \
  tests/integration/cli/test_state_engine_lifecycle_profiles.py \
  tests/e2e/recovery \
  docs/architecture/state_engine/assessments
git commit -m "test(state-engine): complete lifecycle and abandonment proofs"
```

**Definition of Done:**

- [ ] TS-19, F-14, and PB-08 pass. Task 10 lifecycle evidence remains support-only until Task 11 attaches it to every plugin-specific PB-09 case.
- [ ] Finalization-specific RM-14 evidence is linked to Task 7/`elspeth-eefd990b46` without reassigning or prematurely closing that leg.
- [ ] PostgreSQL outcome/abandonment locking has executable race evidence.
- [ ] Effect-linked operation failure is atomic with fenced non-resumable finalization; resumable interruption remains open for the same effect.
- [ ] Follower proof crosses production assembly and plugin traversal.
- [ ] Three SQLite deployment profiles and the live AWS PostgreSQL 16 single-leader profile are separately attributable; PB-08 is N/A outside its two catalog-owned follower profiles.
- [ ] Every lifecycle mode terminates with an honest durable image.

---

### Task 11: Pin the first-party plugin inventory and live-lane contract

**Files:**

- Create: `scripts/state_engine_plugin_matrix.py`
- Create: `tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py`
- Create: `tests/integration/plugins/test_state_engine_plugin_provider_lanes.py`
- Create: `tests/golden/state_engine/plugin_lifecycle_matrix.json`
- Create: `tests/golden/state_engine/evidence_selectors.json`
- Modify: `tests/unit/plugins/test_discovery.py`
- Create: `tests/unit/plugins/test_state_engine_evidence_selectors.py`
- Modify: `tests/unit/architecture/test_state_engine_supported_profiles.py`
- Create: `tests/unit/architecture/test_state_engine_live_evidence_ingest.py`
- Create: `tests/unit/cicd/test_live_provider_acceptance_workflow.py`
- Modify: `tests/conftest.py`
- Modify: `tests/integration/plugins/sources/test_aws_s3_source_live.py`
- Modify: `tests/integration/plugins/sinks/test_aws_s3_sink_live.py`
- Modify: `tests/integration/plugins/transforms/aws/test_bedrock_guardrails_live.py`
- Modify: `tests/integration/plugins/transforms/aws/test_textract_document_analysis_live.py`
- Create or modify as required: maintained live lanes for Azure Blob, Azure AI,
  Dataverse, LLM/provider, Azure Search, and Chroma-backed plugins
- Create: `.github/workflows/live-provider-acceptance.yaml`
- Modify: `pyproject.toml`
- Modify as defects require: first-party plugins under `src/elspeth/plugins/`
- Do not modify: `docs/architecture/state_engine/proof-catalog/v2/catalog.json`
- Create: `docs/architecture/state_engine/proof-catalog/v3/catalog.json`
- Modify: `scripts/state_engine_assessment_lib/common.py`
- Modify: `scripts/state_engine_assessment_lib/catalog.py`
- Modify: `scripts/state_engine_assessment_lib/package.py`
- Modify: `scripts/state_engine_assessment_lib/operations.py`
- Modify: `scripts/state_engine_assessment.py`
- Modify: `tests/unit/architecture/test_state_engine_catalog_contract.py`
- Modify: `docs/architecture/state_engine/completeness-criteria.md`
- Modify: `docs/architecture/state_engine/assessment-framework.md`
- Modify: `docs/architecture/state_engine/assessment-program.md`
- Modify: `docs/architecture/state_engine/proof-catalog/README.md`
- Create: `docs/architecture/state_engine/proof-catalog/v3/README.md`
- Create: `docs/architecture/state_engine/proof-catalog/v3/catalog.schema.json`
- Create: `docs/architecture/state_engine/proof-catalog/v3/assessment.schema.json`

**Step 0: Resolve literal applicability and lane capacity before implementation**

PB-09 currently expands to 51 plugin cases by four required profiles by ten
required dimensions: 2,040 required cells. That Cartesian product conflates
state-store semantics, deployment composition, and provider observation. Pin
the completion path to a new `elspeth-state-engine-v3` catalog identity that
models reviewed per-plugin/profile/dimension applicability explicitly. Freeze
v2 as historical input; never rehash or weaken it in place, and never mark a
v2-required cell N/A in an assessment.

The v3 design must preserve PostgreSQL 16 as required for the maintained AWS
single-leader Landscape deployment. It may narrow a plugin/provider cell only
when its catalog reason agrees with production-owned capability discriminators
and the maintained release lane shows the combination is outside the product contract; it cannot use applicability to
hide missing credentials, a missing test, or an unsupported PostgreSQL path.
Because catalog identity changes, v2 deltas cannot parent a v3 assessment.
Task 12 will publish the first full v3 assessment at the single frozen final
checkpoint.

Pin v3 to catalog `schema_version: 2` and assessment `schema_version: 3`.
For every leg, v3 `required_cases` is an array of objects with this exact
normative shape:

```json
{
  "case_id": "source:llm@openrouter",
  "plugin_key": "source:llm",
  "variant_id": "openrouter",
  "cell_applicability": {
    "sqlite-wal-single-process-leader": {
      "production_entry": {"status": "required", "reason": null},
      "boundary_composition": {
        "status": "not_applicable",
        "reason": "Reviewed product-contract reason"
      }
    }
  }
}
```

Every catalog profile and all ten dimensions must appear exactly once for every
case. `required` must carry a null reason; `not_applicable` must carry a
non-empty reviewed reason. The v3 catalog is the sole authority for required
versus N/A. The golden matrix supplies capability and lane facts that the
catalog validator must reconcile mechanically, but it cannot waive a product
cell. Package validation must reject assessment N/A unless the exact v3 cell is
N/A, and must use an explicit assessment-schema-3 path rather than v2 or the
historical fallback.

`plugin_key` is a non-empty live discovery key for PB-09 cases and JSON `null`
for non-plugin legs. `variant_id` is a closed non-empty identifier and is
`default` when a case has no provider/auth variant. Case IDs must be unique
within their leg; `(plugin_key, variant_id)` must be unique within PB-09.

The checked-in `catalog.schema.json` and `assessment.schema.json` are the
machine-readable normative schemas. Catalog v3 has the exact v2 top-level keys
except `applicability_profiles` is removed; v3 leg records have exactly
`id`, `family`, `title`, `contract`, and object-valued `required_cases`, with no
legacy `applicability_profile`. Assessment schema 3 has the same exact top-level
record set as assessment schema 2, but requires `schema_version: 3`, catalog
`elspeth-state-engine-v3`/schema 2, v3 case IDs in coverage and overrides, and
the completion marker bytes `state-engine-assessment-package-v3\n`.
Assessment N/A is always derived from catalog cells; schema 3 rejects a
hand-authored `not_applicable` override. Schema 3 also replaces the single
global runner assumption with per-evidence `runner`, exact `argv`, and
provenance records. Local records bind the checkout-local Python executable and
platform. GitHub Actions records bind repository, workflow path and digest,
baseline SHA, run ID and attempt, job ID, runner image, selector-lane ID,
artifact digest, and scrub-report digest.

Schema 3 baseline records add an exact, file-by-file `publication_paths` list
(directory entries and globs are forbidden). In current mode the recorded
frozen executable commit `F` and tree must still exist and match exactly. The
validator accepts only (a) `HEAD == F` with worktree changes confined exactly
to the declared prospective publication paths, or (b) a clean `HEAD == P`
where `P` is a single docs-only publication child whose sole parent is `F` and
whose `F..P` path set equals `publication_paths`. It rejects intervening
commits, non-document paths, undeclared files, later descendants, and a changed
tree for `F`. Add pre-publication, post-publication, extra-file, non-doc,
intervening-commit, and stale-tree mutation tests.

Add an explicit version-pair dispatcher and mutation tests:

- catalog v1 / assessment v1 remains historical-only;
- catalog v2 schema 1 / assessment schema 2 remains frozen and valid;
- catalog v3 schema 2 / assessment schema 3 is current;
- every other catalog/assessment pair is rejected.

`init-full` requires an explicit repository-relative `--catalog` option for
every version; there is no implicit current catalog. Task 12 must initialize
with `--catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json`.
Add CLI missing-option, positive, and version-mismatch tests in
`operations.py`/the architecture contract suite.

Provider variants are separate evidence subjects and case IDs. At minimum,
split each supported LLM source/transform provider, RAG Chroma versus Azure
Search, and every supported authentication mode that requires a distinct live
lane. Separate credentialed processes attach to separate variant cases; a
single base plugin case cannot conceal a missing provider. Discovery equality
compares the projected unique `plugin_key` set to the 51-plugin inventory,
while the validator separately requires the exact independently derived closed
variant set described below.

The golden and catalog are not the completeness oracle for variants. Derive the
closed set independently from production-owned configuration discriminators:
the LLM source/transform `Literal` provider unions and provider map, the RAG
`PROVIDERS` registry/config union, and each provider plugin's Pydantic
authentication-mode discriminator/schema. Every derived variant must construct
through the real production config validator. If a claimed mode has no closed
production discriminator, it is not a supported variant until that contract is
added. Compare discovery, golden, and catalog by projected unique `plugin_key`
(exactly 51), then separately compare PB-09 `case_id`/variant pairs to this
independently derived closed set. Use direct owned schema/registry APIs; do not
resolve members through dynamic attribute names.

V1 and v2 catalogs/assessments remain byte- and hash-valid historical inputs.
Only the current v3 catalog is compared with live discovery, so future plugin
additions do not retroactively invalidate frozen historical catalogs. Keep all
global current-catalog pointers on v2 until Task 12 atomically publishes the
first full v3 assessment and switches the hub, proof matrix, and assessment
index.

The v3 transition is lossless outside PB-09. Pin and compare v2's leg IDs and
order, families, titles/contracts, non-PB-09 case IDs and order, profile and
dimension vocabularies, family acceptance rules, hard-gate set and semantics,
evidence record semantics, and execution-profile definitions. Schema 3's
per-evidence runner/provenance and publication-path fields are closed
strengthenings of that evidence contract; they may not remove or relax any v2
requirement. The validator
must prove that every non-PB-09 v2 `(leg, case, profile, dimension)`
required/N/A policy maps identically into v3; no reason or variant mechanism
may narrow it. The only permitted catalog semantic delta is PB-09's reviewed
provider-variant cases and applicability. Add mutations for every family and
specifically PB-11, proving that any other changed, reordered, removed, or
newly N/A contract element is rejected.

Start this task only after Task 10's production lifecycle/follower harness is
available and the Python 3.14 lifecycle blocker `elspeth-61350c4744` is closed
on both maintained Python versions.

**Step 1: Generate and validate an exact inventory from discovery**

Implement `scripts/state_engine_plugin_matrix.py` as the single generator and
validator. Its `check <golden>` command validates schema/keysets and compares
the canonical mechanical projection without writing. Its explicit
`render-skeleton` command preserves already reviewed fields, marks new reviewed
fields `UNCLASSIFIED`, and refuses completion until no marker remains. It must
discover the exact 9 sources, 33 transforms, and 9 sinks and fail on a missing,
duplicate, or unclassified entry. Generate only mechanical facts: exact plugin
key, kind, source/class, determinism, execution model, lifecycle method owners,
sink effect modes, and source hash presence. Keep reviewed facts in the golden
file: provider variants, external-observation requirement, applicable PB
boundaries, local fixture, and release lane.

`tests/unit/plugins/test_discovery.py` must assert exact keyset equality among
live discovery, the golden matrix, catalog inventory, and PB-09 required cases;
counts and representative names are insufficient. Tests compare the canonical
mechanical projection and never rewrite the partly curated golden file.

Commit `evidence_selectors.json` as the non-secret Task 12 handoff. Its
exact schema is `schema_version`, `catalog_id`, and a closed `lanes` array. Each
lane records `lane_id`, `workflow_job`, `profile_case`, `marker_expression`,
exact full `node_ids`, exact `(leg, dimension, case_id, profile_case)` cells,
approved resource-variable names, and artifact kinds—never values. The
selector validator must prove that every executable v3-required cell across
TS, AUX, RC, PB, RM, and F is assigned exactly once to the correct local,
PostgreSQL, or protected-live lane; every node has one proof subject; every
case exists; PB-09 variants exist in the independently derived inventory; and
every live lane maps to one protected workflow job. Catalog-approved N/A cells
must be absent and no selector cell may be invented. Task 12 reads this
catalog-wide manifest rather than reconstructing selectors from prose or
cohort-specific files.

**Step 2: Add RED lifecycle tests**

For every plugin, build settings through `instantiate_plugins_from_config`,
construct `ExecutionGraph` from the real instances, and cross the public
Orchestrator/follower/resume boundary. Exercise constructor/configuration,
`on_start`, `load` or `process`/`accept`, `on_complete`, and `close` as the
plugin kind requires. Route sinks through the recoverable sink-effect
coordinator; direct legacy `write`/`flush` calls are support-only and cannot
satisfy the production lifecycle cell. Assert scheduler, audit, outcome,
effect, artifact, partial-start cleanup, and teardown-order behavior. Local
plugins run hermetically; provider-backed plugins run in their maintained live
acceptance lane.

Each parametrized node binds exactly one plugin case and one execution profile.
Task 10's real same-host follower and CLI/web-hosted lifecycle nodes must be
attached per plugin; a generic lifecycle result cannot silently pass 51 PB-09
cases. PostgreSQL testcontainers remain backend support only and cannot promote
the live AWS deployment profile.

**Step 3: Refuse invalid substitutions**

- A mock plugin cannot satisfy a first-party plugin cell.
- A real first-party plugin with a fake provider may establish internal composition but not the live external-effect case.
- Missing credentials or unavailable services remain `unknown` and block the production-supported verdict.
- One representative plugin may establish a protocol invariant only when the catalog explicitly defines a shared protocol case; it never silently passes another plugin's lifecycle cell.

**Step 4: Run local matrix**

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py \
  tests/unit/plugins/test_discovery.py
```

Expected: all local cases pass; live-provider cases are selected only in their declared release lanes and are never converted to local skips that count as passes.

**Step 5: Run live provider lanes**

Define protected, manually dispatched acceptance environments. Do not dispatch
the new workflow until the policy-tested workflow commit exists on the default
branch; Task 11 local qualification cannot substitute for that activation.
Task 12 performs the sole promotable dispatch against its frozen SHA. The
maintained lanes must cover AWS S3, Bedrock, both
Textract modes, Azure Blob, Azure Content Safety, Azure Document Intelligence,
Dataverse, each supported LLM/provider variant, Azure Search, and Chroma where
the v3 applicability table requires them. The reviewed matrix must also classify
the real `database` sink and decide whether `blob_fetch` and `web_scrape` use a
maintained real-process HTTP fixture or require external observation. Existing
direct `load`, private helper, fake-recorder, and sink-protocol live tests are
support-only until they cross the production boundary above.

Every provider lane that promotes an AWS-deployment PB-09 cell must run through
the real maintained AWS composition with a PostgreSQL 16 Landscape connection;
a real Azure, OpenRouter, Dataverse, or Chroma call from a local runner is still
not AWS-deployment evidence.

Record approved variable names and disposable resource identities in the
evidence `resources` field, never values, URLs, raw provider bodies, or raw
error text. Create uniquely prefixed resources and clean them in `finally`.
Missing credentials or unavailable services remain `unknown`; they are never
reported as local passes. The live workflow must not expose environment secrets
to fork/PR-controlled code.

Declare a strict `live_provider` marker in `pyproject.toml` (keeping
`live_aws` as a compatible narrower selector where useful). The focused
workflow-policy test must pin manual dispatch, a protected environment,
least-privilege permissions, trusted checkout/ref/SHA behavior, absence of
`pull_request` and `pull_request_target`, no untrusted ref input, bounded
artifact retention/redaction, and no secret-bearing command or environment
echo.

Default pytest addopts must exclude `live_provider`. `tests/conftest.py` must
require an explicit operator CLI gate such as `--run-live-provider` before any
marked node can execute, independent of values auto-loaded from `.env`. The
protected workflow is the maintained invoker and must select exact required
nodes; any skip, deselection, warning, xfail, or xpass makes that lane
non-promotable.

At collection, require every live remote-service node—including legacy
`live_aws` and formerly slow-only S3 nodes—to carry `live_provider`; reject a
remote node lacking it. Subprocess tests must prove default deselection and CLI
refusal even when a fixture `.env` contains plausible provider variables.
Pin every workflow action by full commit SHA. A promotable remote artifact must
bind catalog digest, baseline `github.sha`, workflow run ID/attempt, job ID,
runner image, and observed execution profile. Before upload, mechanically scrub
JUnit/profile/stdout/stderr with injected credential and provider-payload
canaries and fail if any canary or configured secret value survives. Package
validation rejects stale/transplanted artifacts whose provenance does not match
the frozen assessment baseline and selector lane.

Add a trusted `ingest-live-evidence <assessment> <artifact-directory> --lane
<lane-id>` operation. The workflow produces a closed artifact manifest plus
JUnit, profile, node-index, and scrub report without printing raw stdout,
stderr, provider payloads, or environment values to the GitHub log. Ingestion
must verify the workflow/run/attempt/job identity, full baseline SHA, workflow
digest, exact selector and collected node set, profile, resource-name allowlist,
artifact digests, result counts, and successful canary/secret scrubbing before
materializing a schema-3 evidence record. Reject missing or extra files,
untrusted producers, lane drift, stale SHAs, and any digest mismatch. Test the
operation with a synthetic digest-bound artifact envelope and mutations;
credentials and provider values never enter the repository or assessment.

The downloaded artifact cannot authenticate its own provenance. The ingest
operation must independently query GitHub's Actions API for the official
run/job/artifact record (or verify an equivalent GitHub artifact attestation)
and bind that trusted response to the envelope, workflow digest, run attempt,
job, artifact identity/digest, protected ref, and frozen SHA. A self-declared
manifest with internally consistent hashes is insufficient. Tests mock the
read-only provenance client and must prove that a fabricated, transplanted, or
re-run envelope is rejected. Use deterministic per-lane publication filenames
so schema-3 `publication_paths` can be frozen before dispatch; do not derive
repository paths from an untrusted run ID.

**Step 6: Fix defects and commit the implementation baseline**

Review `git diff --name-only` against the Task 11 file inventory, including
every newly resolved Azure/LLM/Search/Chroma lane and every defect-driven plugin
file. Construct one explicit `git add` invocation that spells out every
intended file; preserve that exact path list in the Filigree completion comment.
Do not use directory arguments or globs. Then commit with:

```bash
git commit -m "test(state-engine): complete plugin lifecycle inventory"
```

If any plugin source changes, format first and refresh its PH3
`source_file_hash` mechanically with `scripts/cicd/plugin_hash.py` last. Run the
plugin-hash lint and the whole-tree masquerade gate once for the completed
implementation cohort. Run the exact non-inert project Wardline gate for every
touched external-input boundary, including catalog, golden, JUnit/profile, and
workflow-artifact parsers as well as plugin/provider admission. For every source change, compare the
key-free trust-tier finding corpus before and after; do not sign or hand-edit
judge metadata.

Resolve the exact changed paths with `git diff --name-only` and stage them
individually. Do not use broad directory staging for `src/elspeth/plugins` or
`scripts/state_engine_assessment_lib`, because sibling work may be present.

**Step 7: Freeze selector definitions for the final full v3 assessment**

Execute bounded local qualification from the committed selector manifest. The
implementation/golden/workflow commit must be clean before capture. These Task
11 runs are development qualification only: discard their outputs or retain
them outside the repository, and do not cite them as promotable assessment
evidence. Do not publish a v2 delta: the catalog identity changed. After the
workflow is activated on the default branch, Task 12 reruns every local and
remote selector once at the final frozen commit and publishes the sole
promotable first full v3 package. That docs-only
commit may change only `docs/**` and must include the dated assessment plus
`docs/architecture/state_engine/README.md`, `proof-matrix.md`, and
`assessments/README.md`; current-baseline validation cannot be packaged in the
same commit as production/test changes.

Update `test_state_engine_supported_profiles.py` before this freeze so the
maintained-current assertion names v3 and PostgreSQL 16 explicitly, while a
separate immutable-history assertion continues to accept the frozen v2
package. It must fail if a current pointer silently falls back to v2.

**Definition of Done:**

- [ ] Discovery and catalog inventories are identical.
- [ ] Every local first-party plugin has production-boundary lifecycle evidence.
- [ ] Every provider-backed plugin has a closed protected lane, exact selector
  and non-secret resource definition, and remains unknown until Task 12's sole
  promotable live dispatch.
- [ ] V3 explicitly and reviewably resolves every PB-09
  plugin/profile/dimension applicability cell without weakening PostgreSQL 16
  AWS obligations or converting missing evidence into N/A.
- [ ] Task 10 lifecycle evidence is attached to each plugin/profile case rather
  than cited generically; PB-11 remains owned by Tasks 4/6/7.
- [ ] Plugin additions fail the maintained inventory gate until classified and evidenced.

---

### Task 12: Publish the complete assessment and install maintained gates

**Files:**

- Modify: `docs/architecture/state_engine/README.md`
- Modify: `docs/architecture/state_engine/architecture.md`
- Modify: `docs/architecture/state_engine/proof-matrix.md`
- Modify: `docs/architecture/state_engine/assessments/README.md`
- Modify: `docs/architecture/state_engine/proof-catalog/README.md`
- Create: `docs/architecture/state_engine/assessments/<final-timestamp>/README.md`
- Create: `docs/architecture/state_engine/assessments/<final-timestamp>/assessment.json`
- Create: `docs/architecture/state_engine/assessments/<final-timestamp>/evidence.md`
- Create: `docs/architecture/state_engine/assessments/<final-timestamp>/review.md`
- Modify: `.github/workflows/ci.yaml`
- Modify: `.github/workflows/build-push.yaml`
- Create: `tests/unit/cicd/test_state_engine_ci_selection.py`

**Step 0: Install every executable gate before the final freeze**

Finalize and focused-test `.github/workflows/ci.yaml`,
`.github/workflows/build-push.yaml`, the live-provider workflow, selector
manifest enforcement, and `tests/unit/cicd/test_state_engine_ci_selection.py`.
Commit every non-document executable/configuration change first. Require the
manual live workflow to exist on the default branch with its protected
environment before proceeding; otherwise stop with provider cells unknown.

Record the resulting clean commit as the frozen baseline. Any later change to
code, tests, catalog, scripts, configuration, or workflows invalidates every
Task 12 result and restarts Steps 0–5 from a new frozen commit.

**Step 1: Run every maintained evidence cohort at one frozen commit**

Create the first full v3 assessment explicitly:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  init-full "$STATE_ASSESSMENT_ID" \
  "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID" \
  --catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json
```

The final package may
reuse cohort selector definitions, but not their baseline-bound results; rerun
every exact selector from the committed selector manifest at the frozen final
commit. Re-run:

- deterministic SQLite repository and production suites;
- independent-process SQLite recovery/coordination suites;
- PostgreSQL 16 testcontainer suites;
- local first-party plugin lifecycle matrix;
- live provider release lanes;
- full `pytest tests/` CI-equivalent suite.

Record interrupted or unavailable runs as failures/unknowns, never passes.

**Step 2: Run trust and boundary gates**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth
wardline scan . --fail-on ERROR --fail-on-inert \
  --trust-pack scripts.wardline_pack --allow-custom-packs --local-only
```

Trust-tier remains baseline-relative and key-free. Wardline must be run for all external-input changes and any finding fixed at its boundary.

**Step 3: Verify maintained CI/release selection**

- Always-on PR CI: catalog/schema/document validator and deterministic local state-engine contracts.
- Required integration CI: SQLite process matrix and PostgreSQL 16 testcontainer matrix.
- Release acceptance: provider-backed plugin lifecycle cases and any environment-dependent recovery gate.
- Fail when a required case is skipped, deselected, uncollected, or absent from the package.

**Step 4: Assemble the prospective publication and derive the verdict**

Before validation, build the complete docs-only publication overlay: the dated
assessment and retained evidence, hub, architecture summary, proof matrix,
assessment index, and proof-catalog current pointer. Populate
`baseline.publication_paths` with every exact file in that overlay. Do not use a
directory, glob, or post-validation generated file. At this point `HEAD` is
still frozen commit `F`; `git status --porcelain=v2` may contain only those
declared paths. The proof-matrix and hub checks therefore validate the exact
prospective publication rather than the stale pre-assessment documents.

Run:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-package "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID/assessment.json"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  collect-evidence "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID/assessment.json"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py check-links
```

Expected before review:

```text
state-engine assessment: valid (... legs, complete)
```

Do not edit derived totals or the verdict by hand.

**Step 5: Run three independent reviews of the exact overlay**

Use separate readers for:

1. architecture/state/transaction completeness;
2. evidence-to-cell validity and negative-claim discipline;
3. clean-room reproducibility by a future agent.

Resolve every material finding, rerun affected evidence, and require exactly one `Review outcome: complete` line. This is technical review, not a signature or approval package.

If a finding changes any non-document file or the catalog, discard every Task
12 result and restart from Step 0. A docs-only attribution/review correction may
retain unaffected machine artifacts only when their hashes and frozen baseline
remain identical. After any docs correction, update the exact
`publication_paths`/artifact digests as applicable, rerun validation and link
checking, and have the affected reviewer inspect the final overlay.

**Step 6: Commit the reviewed overlay and validate the published state**

The already reviewed hub and proof matrix record:

- frozen executable commit/tree `F` (the publication commit cannot
  self-reference its own hash);
- catalog ID and digest;
- Landscape schema epoch;
- supported state stores/deployments/lifecycle modes;
- exact complete leg/gate counts;
- compiler handoff rule: a future `CompiledPipeline` binds the catalog ID/digest and may execute only when runtime assembly reports a compatible state-engine contract.

Do not create a signed plan package, approval sidecar, or document hash manifest. The catalog digest is a runtime compatibility fact; evidence artifact hashes exist only where reproducibility needs them.

```bash
git add docs/architecture/state_engine/README.md \
  docs/architecture/state_engine/architecture.md \
  docs/architecture/state_engine/proof-matrix.md \
  docs/architecture/state_engine/assessments/README.md \
  docs/architecture/state_engine/proof-catalog/README.md \
  "docs/architecture/state_engine/assessments/$STATE_ASSESSMENT_ID"
git commit -m "feat(state-engine): publish the complete v3 contract"
```

Require the publication commit `P` to have sole parent `F`, then rerun
`validate-package`, `collect-evidence`, and `check-links` from the clean `P`.
Schema-3 docs-only-descendant validation must derive the same complete verdict
and prove the committed `F..P` path set exactly equals `publication_paths`.
Do not amend or add another descendant; any change requires a new frozen run.

**Step 7: Close the tracker tree**

Only after the clean published-state validation passes, close issues whose exit
gates are represented by passing cells. Keep future multi-replica work
separate. Record `F`, `P`, the catalog digest, workflow run identities, and the
exact final validator output in the completion comment.

**Definition of Done:**

- [ ] The assessment validator derives `complete` at the final commit.
- [ ] Every required cell is pass or catalog-approved N/A.
- [ ] Every required provider-backed plugin/variant has current live evidence
  bound to the frozen SHA, protected workflow provenance, and the real required
  execution profile.
- [ ] Every hard gate is closed.
- [ ] CI/release selection makes future drift fail closed.
- [ ] Independent review is complete with no unresolved material finding.
- [ ] The hub, source, catalog, deployment docs, tracker, and compiler handoff agree.

---

## Verification commands for each integration checkpoint

Run these after every merged cohort, not only at the end:

```bash
git status --short --branch
PYTHONPATH="$PWD/src" .venv/bin/python -c 'import elspeth; print(elspeth.__file__)'
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q -n 0 \
  tests/unit/architecture/test_state_engine_supported_profiles.py \
  tests/unit/architecture/test_state_engine_catalog_contract.py
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json
git diff --check
```

Use the v2 catalog path at checkpoints before the Task 11 v3 commit; use v3 at
every checkpoint after it. The Task 12 final checkpoint is v3 only.

At the final checkpoint, additionally run the exact full suite, PostgreSQL lane, process matrix, plugin acceptance lanes, trust-tier corpus comparison, Wardline gate, package validator, link validator, and independent reviews described in Task 12.

## Stop conditions

Stop and surface the result rather than narrowing the claim when:

- source, deployment, ADR, and catalog profiles disagree;
- a required backend/provider is unavailable;
- an independent-process scenario is replaced by threads or exception injection;
- a test proves only repository behavior while the cell requires production composition;
- a failure reveals a new state/subtype or cross-transaction seam absent from the current catalog;
- any required selector skips, times out, or is not collected;
- Loomweave is stale for a reachability claim or Warpline is stale for a blast-radius claim;
- Filigree returns `SCHEMA_MISMATCH`;
- a proposed fix requires an operator signing key.

In these cases, keep the verdict `not_complete` or `insufficient_evidence`, update the exact gap owner and exit gate, and continue with other independent cohorts where safe.
