# DAG Corpus Wave B Pre-Platform Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add honest production runtime, audit, and recovery evidence for the
platform-independent DAG corpus, reconcile scale to its existing dedicated
task, and preserve every distributed-authority cell for the later
deferred-platform rebase.

**Architecture:** `release/0.7.2` now contains the reviewed Wave A typed
config/build boundary. Wave B proceeds on `codex/dag-corpus-wave-b` through
small, separately reviewed corpus children: first strengthen the common
evidence contracts, then add scenario-specific runtime/audit and recovery
evidence. No packet may treat single-process execution as concurrency or scale
evidence, or change production code merely to satisfy the corpus.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, pytest, SQLAlchemy,
ELSPETH `ExecutionGraph`, `Orchestrator`, `LandscapeDB`, checkpoint/recovery
APIs, Filigree, Loomweave, Warpline, Ruff, and mypy.

---

## Frozen sprint boundaries

- [x] Wave A is fast-forwarded into `release/0.7.2` at
  `5308e8c8867c6c6c26964a82eaf1081e2d1327d0`.
- [x] The merged release and the isolated Wave B worktree each pass the
  191-test corpus baseline.
- [x] Deferred platform is paused, clean, and unmerged at
  `132bd53232ea6b3885250675c361d3c057b19ac5`.
- [ ] Keep `codex/deferred-platform-completion` read-only during this sprint.
- [ ] Keep all multi-process ownership, lease expiry, reclaim,
  late-completion, peer-cancellation, and distributed-finalization cells
  non-pass until that branch is rebased and resumed.
- [ ] Keep the parent `elspeth-ef29ef6ba4` open. Corpus-only implementation
  units receive bounded children unless the live tracker already has an
  authoritative issue for that exact proof boundary.
- [ ] The coordinator is the sole committer/integrator of
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`. Except for the
  serialized F0 migration, worker branches change fixtures, harness/plugins,
  and focused tests only; they return the exact intended YAML delta for the
  coordinator to apply after integration and verification.
- [ ] Start every packet from the latest integrated Wave B SHA. Before work,
  verify its fixed tracker ID, status, merge ancestry, and evidence IDs; if it
  is already integrated, reverify and skip rather than duplicating it.
- [ ] Immediately before final review/integration, rebase each worker branch
  onto the latest integrated Wave B SHA and rerun its focused gate. This is
  mandatory because recovery lanes share harness and integration-test hot
  files even when their fixture directories are independent.

## Required common invariants

Every executable case must:

1. load canonical YAML with no environment interpolation;
2. instantiate production plugins in the correct fresh/resume mode;
3. construct and validate the production `ExecutionGraph`;
4. assemble the production `PipelineConfig`;
5. use `Orchestrator.run` or the public resume path with a real `LandscapeDB`;
6. assert exact rows, identities, routes/dispositions, durable outcomes, and
   audit facts declared by a typed expectation;
7. use a fresh database reopen and fresh runtime objects for recovery proof;
8. promote only the lifecycle stages the harness actually completed; and
9. add no skip, xfail, direct-graph substitute, platform shortcut, or
   documentary-only pass evidence.

Runtime/audit evidence must use a canonical stable projection. Volatile UUIDs
and timestamps may be normalized only into deterministic logical keys derived
from durable source/row ordinal, node, branch/output ordinal, and parent/path
relations. The expected projection includes exact token lineage, routes,
terminal outcomes/dispositions, scheduler work, per-sink artifacts, and the
material audit fields that correlate exported explanation to durable state.
Aggregate record-type counts alone cannot promote runtime or audit.

## Dependency DAG and restart guard

```text
F0 -> B1-R -> B1-X{linear,roots,queue,route}
F0 -> B2-{5,6,78} -> B2-X{terminal,coalesce,sequential,parallel}
F0 -> B3-R -> B3-X
F0 -> B4-A
all completed Wave B packets -> deferred-platform rebase handoff
```

The four main lanes may be developed independently after F0, but manifest
promotion is serialized by the coordinator. A child is not closed until its
code is integrated, its manifest delta is applied, and the resulting Wave B
head passes the corpus gate.

New-case worker branches test their proposed case before manifest integration
through a focused explicitly constructed `HarnessCaseSpec` or temporary
single-case manifest fixture. After integration, only the canonical manifest-
driven parametrization is acceptance evidence; the coordinator must run it
before applying cell promotions or closing the child.

## Authoritative tracker ledger

| Scope | Tracker ownership |
| --- | --- |
| F0, B1-R, B1-X topology packets, B2 runtime/recovery packets, B3 aggregation/expansion, B4-A | New bounded children of `elspeth-ef29ef6ba4` |
| Scale envelope and mandatory CI | Existing `elspeth-cb1053fe46`; no Wave B duplicate and no scale promotion in this sprint |
| Transform/gate scheduler disposition | Existing `elspeth-2e66723070` and `elspeth-6f6bbbec00`; B3 consumes verified evidence or records explicit dependencies |
| Full sink bundle/redrive | Existing `elspeth-76bb92bc7d`; claim and close this authoritative task rather than creating a child duplicate |
| Multi-process registered orchestration/sink redrive | Existing `elspeth-9a52eb80f9`; deferred to B4-B after platform rebase |
| Checkpoint implementation identity and post-leadership cleanup | `elspeth-f321e3ff21` and `elspeth-245b21351b` on the paused platform branch |

## Packet map

| Packet | Scope | Immediate promotion ceiling | Deferred |
| --- | --- | ---: | --- |
| F0 | Typed plural-input, multi-output, runtime/disposition, and audit evidence contracts | Infrastructure only | None |
| B1-R | Linear, independent roots, queued fan-in, conditional routing runtime/audit | 8 cells | Recovery, concurrency, scale |
| B1-X | Topology-specific crash/reopen/public-resume for B1 | 4 cells | Concurrency, scale |
| B2-5 | Fork to multiple terminals with one deterministic terminal failure | S5 runtime/audit plus supported contracts/build facts | Concurrency, recovery, scale |
| B2-6 | Fork/coalesce 4-policy x 3-merge matrix and failure matrix | S6 contracts/runtime/audit | Concurrency, recovery, scale |
| B2-78 | Sequential/nested and parallel coalesces | S7/S8 build/contracts/runtime/audit | Concurrency, recovery, scale |
| B2-X | Four independently stoppable topology recovery packets for S5-S8 | 4 recovery cells | Concurrency, scale |
| B3-R | Aggregation, expansion, disposition, and sink runtime/audit | Scenario-specific cells listed below | Named product blockers |
| B3-X | Aggregation, expansion, and sink recovery | Scenario-specific cells listed below | Multi-process sink redrive |
| B4-A | Checkpoint control-vs-reopen/resume equivalence | Runtime and audit only | Contracts/recovery blockers, concurrency, scale |
| B4-B | Multi-worker lease/reclaim/late completion | 0 during this sprint | Entire packet until platform rebase |
| Scale | External dedicated envelope/CI work | 0 during this sprint | `elspeth-cb1053fe46` |

## Plan review outcome

Reality, architecture, quality, and systems review all approved this plan after
the following blocking corrections were incorporated:

- F0 became an atomic six-case manifest/schema/fixture migration;
- exact stable runtime, durable-state, lineage, route, disposition, and audit
  projections replaced aggregate-count-only evidence;
- scale was reconciled to `elspeth-cb1053fe46` with no Wave B promotion;
- manifest mutation and worker integration were made serial and restart-safe;
- existing scheduler, sink-redrive, checkpoint, and multi-process tracker
  ownership was preserved without duplicates; and
- every pre-platform recovery proof was made provisional and subject to full
  post-rebase invalidation/reacceptance.

## Task 1: F0 — exact common evidence contracts

**Files:**

- Coordinator modify atomically in the F0 integration commit:
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify: `tests/fixtures/dag_scenario_corpus/schema.py`
- Modify: `tests/fixtures/dag_scenario_corpus/loader.py`
- Modify: `tests/fixtures/dag_scenario_corpus/harness.py`
- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`
- Modify: all six YAML fixtures registered by the current manifest
- Add distinct per-source inputs for `multiple-independent-sources` and
  `multi-source-queue-fan-in`
- Modify only if registration is required:
  `tests/fixtures/dag_scenario_corpus/plugins.py`

F0 is a serialized, coordinator-owned migration barrier. No later packet may
start until its commit is integrated and verified.

- [ ] **Step 1: Write RED model tests for exact plural input attribution.**

  Replace `HarnessCaseSpec.input_fixture` with an immutable, sorted
  `input_fixtures: Mapping[source_name, repository_relative_fixture]` contract,
  and add an immutable, sorted
  `output_artifacts: Mapping[sink_name, safe_relative_filename]` declaration
  for trusted per-sink artifact binding. Relative filenames must be unique,
  traversal-free leaves resolved beneath the per-case temporary directory.
  RED tests must reject empty mappings, unsorted serialization, missing or
  extra source names, duplicated resolved paths when distinct fixtures are
  declared, a literal token present only in a comment, and a rendered source
  path that differs from its declared source-name binding. They must also
  reject missing/extra sink names, input/output token-name collisions, and any
  sink path that differs from its declared binding.

- [ ] **Step 2: Run the focused attribution tests and capture the expected
  failures before changing schema/loader/harness code.**

  Run:

  ```bash
  env -u VIRTUAL_ENV uv run --frozen pytest --collect-only -q -n 0 \
    tests/unit/architecture/test_dag_scenario_corpus_contract.py \
    tests/integration/core/dag/test_dag_scenario_production_path.py \
    -k 'plural_input_artifact_binding or per_sink_artifact_binding or exact_runtime_projection'

  env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
    tests/unit/architecture/test_dag_scenario_corpus_contract.py \
    tests/integration/core/dag/test_dag_scenario_production_path.py \
    -k 'plural_input_artifact_binding or per_sink_artifact_binding or exact_runtime_projection'
  ```

  Name every new RED test under one of those three prefixes, record the exact
  non-zero collection count, and then record the expected failures before any
  implementation change.

- [ ] **Step 3: Implement exact source-name binding and deterministic hashing.**

  `render_settings()` must substitute deterministic trusted tokens
  `input_<normalized_source_name>` and `output_<normalized_sink_name>`, with
  normalization and collision validation. It must load the rendered settings,
  compare exact configured `source_name -> resolved input path` and
  `sink_name -> resolved artifact path` maps to their declarations, and hash
  the YAML bytes plus each sorted
  `(source_name, fixture_path, fixture_bytes)` tuple. Preserve fail-closed
  behavior from `elspeth-e8acea2a55`; reserve `fault_marker` exclusively for
  fault injection instead of using it as a second output path.

  Migrate all six manifest cases and YAML fixtures atomically, including the
  pinned case dictionaries, fixture hashes, and evidence-registry digest.
  Distinct orders/refunds fixtures must be introduced here so the multi-source
  binding itself is proved before B1 runtime promotion.

- [ ] **Step 4: Write RED typed-expectation tests for multiple sink artifacts,
  exact runtime outcomes/dispositions, and exact audit counts.**

  Add an explicitly discriminated immutable run expectation/evidence model
  whose canonical equality is directly assertable. Required fields are:

  - exact per-sink canonical output rows or fixture digest;
  - exact `rows_processed`, `rows_succeeded`, and `rows_failed`;
  - exact logical token keys, parent/child lineage, and no orphan/duplicate
    tokens;
  - exact terminal outcomes/dispositions keyed by a closed vocabulary;
  - exact route/path and scheduler-work projections where applicable;
  - a stable durable repository projection and matching portable audit-export
    projection;
  - sorted exact material audit facts and source-load count.

  The models must reject duplicate keys, unsorted forms, total/count
  mismatches, runtime facts on unattempted evidence, and expectation/workflow
  mismatches.

- [ ] **Step 5: Implement the minimal typed models and evidence extraction.**

  Use the existing linear run as the end-to-end tracer bullet, then prove the
  plural-source and conditional multi-output bindings through focused cases.
  Extract observed facts from the public `RunResult`, per-sink output
  artifacts, recorder/repository query surfaces, and `LandscapeExporter`. Do
  not query private tables when a public repository/query method exists. Keep
  the existing Wave A build evidence shape unchanged.

- [ ] **Step 6: Re-run the focused RED tests, then the full corpus gate and
  static checks.**

  Expected result: all existing 191 tests plus the new contract tests pass.
  Also require zero remaining singular `input_fixture:` declarations, zero
  `${input_csv}` tokens in registered YAML, no sink path using
  `${fault_marker}`, and exact evidence-registry digest parity.

- [ ] **Step 7: Obtain independent spec and quality review, repair findings,
  commit, comment, and close the F0 child.**

## Task 2: B1-R — source, fan-in, and routing runtime/audit

**Files:**

- Coordinator modify after the B1-R child integrates:
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`
- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`
- Modify fixtures below:
  `tests/fixtures/dag_scenario_corpus/v1/linear/`,
  `multiple-independent-sources/`, `multi-source-queue-fan-in/`, and
  `conditional-routing/`

- [ ] **Step 1: Write the exact B1 runtime table first.**

  Cases:

  - `linear:happy-path`: exact three output rows and exact runtime/audit facts;
  - `multiple-independent-sources:independent-roots`: distinct
    `orders.csv`/`refunds.csv`, exact six rows and per-source attribution;
  - `multi-source-queue-fan-in:queued-fan-in`: distinct sources, exact six
    globally ordered outputs, queue traversal, and source attribution;
  - `conditional-routing:two-way-gate`: exact accepted/rejected artifacts,
    2/1 split, route labels, dispositions, and audit facts.

- [ ] **Step 2: Run the B1 table RED.**

  It must fail because the build-only cases do not yet produce runtime/audit
  evidence or the exact plural-source/output facts.

- [ ] **Step 3: Retain `linear:happy-path` as `run`, convert the other three
  build cases to `run`, update fixtures, and implement the minimum
  case-independent harness support.**

- [ ] **Step 4: Add production-builder negatives for missing and invalid gate
  destinations.**

  These pytest references may support config/build/contracts only. They do not
  support runtime by analogy.

- [ ] **Step 5: Promote exactly the four B1 runtime and four audit cells.**

  Leave B1 recovery, concurrency, and scale cells unchanged in this commit.

- [ ] **Step 6: Run focused B1 tests, full corpus, static checks, Warpline,
  reviews, commit, and close B1-R.**

## Task 3: B1-X — independently stoppable topology recovery packets

**Files:**

- Coordinator modify after child integration:
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify: `tests/fixtures/dag_scenario_corpus/harness.py`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`
- Add fault-enabled fixtures beside each B1 scenario fixture

- [ ] **Step 1: Add a typed recovery-fault declaration and RED dispatcher
  tests.**

  Supported B1 seams must be named, closed-vocabulary values. A recovery case
  cannot silently use the checkpoint scenario's EOF seam when its topology
  requires a different durable boundary.

- [ ] **Step 2: Execute four separate child/commit/review cycles.**

  `reopen-resume-after-source`, `independent-roots-reopen-resume`,
  `queued-fan-in-reopen-resume`, and `route-reopen-resume` must each close the
  first database, rebuild settings/plugins/graph/config with fresh objects,
  use the public recovery/resume path, and prove canonical output exactly
  once, one source load per declared source, terminal work/outcomes, resume
  marker, and checkpoint cleanup.

  Before reopening, each case must prove its named fault seam was reached
  exactly once, the run is interrupted at the expected status/checkpoint and
  topology, the intended durable token/work state exists, and no premature
  sink artifact was published.

- [ ] **Step 3: Implement only production-path fault plumbing in corpus test
  infrastructure.**

  If a required durable seam is absent in production, stop and create a
  Filigree bug; do not inject state directly to make the corpus pass.

- [ ] **Step 4: Promote one recovery cell after each topology child passes its
  own review and gate.**

  Recovery evidence remains provisional at its recorded Wave B SHA until the
  post-platform invalidation and rerun described in Task 12.

## Task 4: Scale boundary — consume the dedicated issue, do not duplicate it

**Files:**

- Coordinator-only manifest owner retarget:
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify pinned manifest contract expectations and evidence-registry digest
- No status or evidence promotion and no production/test-harness implementation
- Existing owner: `elspeth-cb1053fe46`

- [ ] **Step 1: Verify `elspeth-cb1053fe46` remains the authoritative scale
  task and record it on every unchanged scale cell.**

- [ ] **Step 2: Do not create B1-S/B2-S children and do not promote scale in
  this sprint.**

  The existing task requires the repository-standard
  `tests/performance/scalability/` location, an exact enforced CI/release-gate
  job, fixed runner/environment identity, fresh database/artifact/payload paths
  per repetition, warmup/sample count, retained raw results, a deterministic
  threshold with margin, and safe above-envelope failure behavior.

- [ ] **Step 3: Leave all scale cells honest and incomplete for the later
  dedicated issue.**

## Task 5: B2-5 — partial terminal failure

**Files:**

- Coordinator modify after child integration:
  `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify: `tests/fixtures/dag_scenario_corpus/plugins.py`
- Add fixtures under:
  `tests/fixtures/dag_scenario_corpus/v1/fork-multiple-terminals-partial-failure/`
- Modify the corpus integration and contract tests

- [ ] **Step 1: RED the `one-terminal-fails` production case.**

  Require `completed_with_failures`, the surviving artifact, the failed
  terminal disposition, exact parent links, no orphan tokens, and exact audit
  export.

- [ ] **Step 2: Add a deterministic failing sink through the public corpus
  plugin manager and the canonical YAML fixture.**

- [ ] **Step 3: Promote only the facts proved by the runtime/audit case.**

  Recovery, concurrency, and scale remain for their own packets.

- [ ] **Step 4: Review, gate, commit, and close B2-5.**

## Task 6: B2-6 — coalesce policy and merge-strategy matrix

**Files:**

- Modify plugins/integration/contract files listed above; coordinator applies
  the manifest delta after child integration
- Add fixtures under:
  `tests/fixtures/dag_scenario_corpus/v1/fork-coalesce-policies/`

- [ ] **Step 1: RED the exact 4 x 3 positive matrix.**

  Cover `require_all`, `first`, `quorum`, and `best_effort` crossed with
  `union`, `nested`, and `select`. Quorum/best-effort inputs must actually lose
  a branch; select must choose an arrived branch.

- [ ] **Step 2: RED four failure cases and union-collision policy cases.**

  Required failures: `require-all-lost-branch`, `quorum-impossible`,
  `best-effort-all-lost`, and `first-all-lost`. Require stable reasons and
  dispositions. Cover `last_wins`, `first_wins`, and `fail` collisions through
  production-path pytest evidence.

- [ ] **Step 3: Implement deterministic branch failure only in the public
  corpus plugin manager; add fixtures and exact evidence.**

- [ ] **Step 4: Promote only S6 contracts/runtime/audit facts proved by the
  matrix, then review, gate, commit, and close B2-6.**

## Task 7: B2-78 — sequential and parallel coalesces

**Files:**

- Add fixtures under `sequential-nested-fork-coalesce/` and
  `parallel-coalesces/`
- Modify corpus tests; coordinator applies the manifest delta after child
  integration

- [ ] **Step 1: RED `two-sequential-require-all`.**

  Require exact output, 3 processed/3 outputs, 21 tokens, 9 edges, schema
  propagation through the second merge, and a negative incompatible-schema
  build case.

- [ ] **Step 2: RED `two-parallel-require-all`.**

  Require 3 inputs/6 terminal outputs, 21 tokens, exact dual artifacts, and
  graph shape: 6 nodes, 8 edges; source=1, gate=1, coalesce=2, sink=2. Add a
  negative proving branch names cannot cross or be reused between coalesces.

- [ ] **Step 3: Implement fixtures, exact evidence, and only the manifest
  promotions those cases prove.**

- [ ] **Step 4: Review, gate, commit, and close B2-78.**

## Task 8: B2-X — independently stoppable topology recovery packets

**Files:**

- Modify B2 fixtures/harness/tests
- Coordinator applies the exact manifest delta after each child integrates

- [ ] **Step 1: Execute four separate child/commit/review cycles.**

  Cover partial-terminal resume, held coalesce branch, between sequential
  merges, and one-parallel-barrier-complete/one-pending. Require fresh-object
  reopen, public resume, no duplicates, terminal outcomes, and cleanup.

  Before reopening, prove the named fault seam was reached exactly once, the
  expected interrupted status/checkpoint/topology and durable work state are
  present, and no premature sink artifact was published.

- [ ] **Step 2: Promote one B2 recovery cell only after its topology child is
  integrated and independently reviewed.**

- [ ] **Step 3: Mark each recovery evidence set provisional at its exact Wave B
  SHA pending the post-platform invalidation/rerun.**

- [ ] **Step 4: Keep all eight B1/B2 concurrency cells non-pass and owned by
  the later platform-rebase packet; keep all scale cells non-pass under
  `elspeth-cb1053fe46`.**

## Task 9: B3-R — stateful runtime and audit

**Files:**

- Add scenario fixtures under aggregation, row expansion, disposition, and
  sink-redrive fixture directories
- Modify plugins/corpus tests; coordinator applies the manifest delta after
  child integration

- [ ] **Step 1: RED exact aggregation and expansion cases.**

  `eof-immutable-membership` produces exactly `{value: 60, count: 3}` with
  immutable membership and stable ordinals. `json-explode-parent-child`
  produces exactly six children from three parents with exact identities,
  groups, outcomes, and lineage.

- [ ] **Step 2: RED four separate disposition cases.**

  `retry-then-success`, `source-quarantine-routed`, `transform-discard`, and
  `transform-error-route` each require exact attempts, route, artifact,
  disposition, and audit facts. Keep the scenario contracts cell partial while
  `elspeth-67b44040ee` is open. Treat `elspeth-2e66723070` and
  `elspeth-6f6bbbec00` as the authoritative scheduler-disposition proof: either
  consume their already integrated verification or record explicit
  dependencies and leave affected cells unchanged.

- [ ] **Step 3: RED sink `write-once` and implement the minimum fixtures.**

- [ ] **Step 4: Apply the exact per-scenario ceiling, review, gate, commit, and
  close B3-R.**

  - `aggregation-immutable-batch`: contracts/runtime/audit may pass;
  - `row-expansion-parent-child-recovery`: contracts/runtime/audit may pass;
  - `retry-quarantine-discard-routed-errors`: runtime/audit may pass, contracts
    stays partial while `elspeth-67b44040ee` is open;
  - `sink-write-pending-redrive`: runtime/audit may pass after `write-once`;
  - every B3 recovery, concurrency, and scale cell remains unchanged here.

## Task 10: B3-X — stateful recovery

**Files:**

- Modify B3 fixtures/harness/tests; coordinator applies manifest deltas after
  child integration
- Reuse public recovery and sink-effect query surfaces

- [ ] **Step 1: Create a bounded aggregation/expansion corpus child and retain
  the closed `elspeth-7cdc4da434` evidence as historical input, never as the
  active owner.**

- [ ] **Step 2: RED aggregation and expansion reopen/resume cases.**

  Cover EOF/post-barrier immutable aggregation recovery and the
  child-enqueue/parent-disposition expansion seam. Require no remint/replay and
  exact terminal state.

- [ ] **Step 3: Claim and close, do not duplicate, `elspeth-76bb92bc7d` for
  `pending-redrive-reopen`.**

  The case must prove payload, sink, outcome, path, error hash/message,
  attempt/effect identity, lease clearing/reclaim, recovery event, exactly one
  publication, and no source replay. Do not promote sink recovery until that
  authoritative task is integrated, verified, and closed.

- [ ] **Step 4: Keep multi-process sink redrive owned by
  `elspeth-9a52eb80f9` and keep B3 scale/concurrency non-pass.**

- [ ] **Step 5: Review, gate, commit, and close only evidence-complete children.**

  Aggregation, expansion, and sink recovery evidence remains provisional at
  its recorded Wave B SHA pending the post-platform invalidation/rerun.

## Task 11: B4-A — checkpoint control/resume equivalence

**Files:**

- Modify checkpoint fixture, harness, and corpus tests; coordinator applies the
  manifest delta after child integration

- [ ] **Step 1: RED `control-vs-reopen-resume`.**

  Run an uninterrupted control and a faulted copy in separate databases.
  Destroy runtime objects, reopen, rebuild, and resume publicly. Compare
  canonical outputs and stable durable/audit projections while excluding only
  declared volatile IDs/timestamps.

- [ ] **Step 2: Require one source load, one effective sink output, terminal
  work/outcomes, resume marker, checkpoint removal, and exact
  `NonResumableRunError` refusal on a second resume attempt.**

  Capture durable and portable-export projections immediately before and
  after the refused second resume and require byte-for-byte canonical equality
  so the refusal proves zero mutation.

- [ ] **Step 3: Promote checkpoint runtime and audit only.**

  Keep contracts/recovery partial while `elspeth-f321e3ff21` and
  `elspeth-245b21351b` remain open. Keep concurrency and scale unknown.
  Runtime/audit evidence is provisional at its recorded Wave B SHA pending the
  post-platform invalidation/rerun.

- [ ] **Step 4: Review, gate, commit, and close B4-A.**

## Task 12: Deferred-platform rebase handoff

**Files:**

- Modify: `docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-handoff.md`
- Modify: this plan with final completed/deferred ledger
- Modify manifest reasons/owners only when live tracker state supports it

- [ ] **Step 1: Recompute the exact live manifest ledger and list every
  remaining concurrency/distributed-authority cell.**

- [ ] **Step 2: Record the combined release/Wave B SHA and all packet commits,
  tests, reviews, and open blockers.**

- [ ] **Step 3: Give the paused platform branch an exact rebase instruction.**

  Rebase `codex/deferred-platform-completion` onto the final combined
  `release/0.7.2`/Wave B integration commit, rerun its impacted authority
  suites, then implement B4-B using its real independent-process PostgreSQL
  harness. Do not retain any pre-rebase acceptance hash.

  B4-B must launch independent registered production orchestrator/scheduler
  workers against shared PostgreSQL, assert Landscape claim epochs and
  stale-worker fencing, and consume `elspeth-9a52eb80f9`. Web session ownership
  is not DAG engine ownership. A scenario concurrency cell may pass only when
  that topology is itself exercised; scenario 15 evidence cannot promote B1
  or B2 concurrency by analogy.

- [ ] **Step 4: Invalidate and reaccept every pre-platform recovery proof.**

  Treat B1-X, B2-X, B3-X, and B4-A evidence as provisional at their recorded
  Wave B SHAs. After rebase, rerun every registered recovery case plus the full
  corpus gate, regenerate topology/compatibility evidence, and re-audit every
  promoted recovery/runtime/audit cell. No evidence locator, fixture hash,
  acceptance hash, or cell status is copied forward merely because the rebase
  is textually clean.

- [ ] **Step 5: Record the paused branch merge base and exact replay range in
  the handoff.**

- [ ] **Step 6: Run final corpus/static/Warpline review and release the parent
  claim without closing the incomplete umbrella.**

## Per-child verification floor

Use focused RED/GREEN tests during implementation. Before each child close:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen ruff check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

env -u VIRTUAL_ENV uv run --frozen mypy \
  tests/fixtures/dag_scenario_corpus \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py \
  tests/integration/core/dag/test_dag_scenario_production_path.py

git diff --check
git status --short --branch
```

Run additional scenario-specific recovery suites and every Warpline
reverification item affected by that child. Scale is outside this sprint.

The full repository gate is reserved for the final combined milestone and is
the local equivalent of the required CI lanes:

```bash
env -u VIRTUAL_ENV uv run --frozen ruff check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen mypy src/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen pytest tests/ -v \
  -m "not slow and not stress and not performance and not testcontainer"
```
