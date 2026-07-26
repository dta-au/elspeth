# DAG Corpus Wave B Autonomy Handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` for each bounded packet and
> `superpowers:verification-before-completion` before every tracker close or
> integration. Use the project-local Filigree, Loomweave, and Warpline
> workflows at their normal gates. Keep one coordinator as the sole manifest
> integrator.

**Goal:** Resume the maintained DAG scenario corpus from the verified Wave A
boundary, integrate it with the finalized deferred-platform runtime, and close
the remaining 109 applicable non-pass cells with executable production-path
evidence or an honest tested rejection contract.

**Architecture:** Wave A remains the typed configuration-to-production-build
foundation. Wave B adds production runtime, audit, recovery, checkpoint, and
multi-worker evidence only after the deferred-platform implementation boundary
and full two-process matrix are stable. The preferred lane integrates before
candidate review/freeze; any later integration invalidates and repeats the
complete acceptance sequence. Independent authoring and product-capability
packets remain separate so they cannot silently weaken runtime or tracker
ownership boundaries.

**Tech Stack:** Python 3.12/3.13, Pydantic v2, PyYAML, pytest, Elspeth's
production DAG builder, Orchestrator, Landscape, PostgreSQL/SQLite, Filigree,
Loomweave, Warpline, Ruff, and mypy.

---

## Current status

This handoff is operational guidance, not release evidence or an approval
receipt. Refresh every volatile fact before acting.

Recorded on 2026-07-26:

- Wave A branch: `codex/dag-corpus-wave-a`
- Wave A base: `release/0.7.2@2a3452e3452581008b5fb61b1e75ad0b8f03fb2f`
- Wave A implementation/evidence head:
  `f6920ea9a43e99953a9b5e9efa7dec463f1851a3`
- Focused corpus gate: `191 passed`
- Full branch head: refresh with `git rev-parse codex/dag-corpus-wave-a`;
  handoff-only commits follow the implementation/evidence head
- Manifest verdict: `not_complete`
- Parent: `elspeth-ef29ef6ba4`, open and `in_progress`
- Closed Wave A children:
  - `elspeth-e8acea2a55` at
    `5e799c06605ae8ddcd896a338189b5669672bc06`
  - `elspeth-d88d0e45c0` at
    `90e218f2bbe117e69157e4cd70238f238d179bf5`
  - `elspeth-a77a50d44d` at
    `be5f183aa7735adb3b5e3291709855056b54a68f`

The deferred-platform worktree is **not complete** and continues to advance.
Do not record one of its transient commits as the Wave B trigger. Until that
branch is integrated, the live plan authority is the file in
`/home/john/elspeth/.worktrees/deferred-platform-completion` on branch
`codex/deferred-platform-completion`:

```bash
PLATFORM_WORKTREE=/home/john/elspeth/.worktrees/deferred-platform-completion
git -C "$PLATFORM_WORKTREE" status --short --branch
git -C "$PLATFORM_WORKTREE" rev-parse HEAD
sed -n '1,$p' \
  "$PLATFORM_WORKTREE/docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md"
```

Read the entire live file before choosing an integration lane. The relative
[deferred-platform plan copy](2026-07-26-finish-deferred-deployment-platforms.md)
on this Wave A branch ends at Task 17 and is stale, historical, and
non-authoritative. After rebasing onto an implementation or release commit
that contains the active plan, reread the merged copy from that combined
worktree before executing it.

Authoritative inputs:

- [Wave A design](../specs/2026-07-26-dag-corpus-wave-a-design.md)
- [Wave A implementation plan](2026-07-26-dag-corpus-wave-a.md)
- Active deferred-platform plan in the worktree and branch named above; the
  relative Wave A copy is retained only for history
- [Maintained corpus manifest](../../architecture/dag/scenario-corpus/v1/manifest.yaml)
- [DAG completeness criteria](../../architecture/dag/completeness-criteria.md)

## Integration lanes and start boundary

Choose exactly one lane from current live state. Both lanes require the active
plan's complete two-process acceptance matrix. Neither lane treats a transient
Task commit as a durable trigger.

### Lane A — preferred pre-freeze integration

Pause the platform branch after every implementation and candidate-boundary
task through Task 20 is committed and verified, including the full
two-process failure, cancellation, and race matrix, but **before Task 21
review/freeze begins**. At that clean boundary, capture the implementation SHA
for this integration session, rebase Wave A, complete the Wave B packets, and
merge the combined corpus into `codex/deferred-platform-completion`. Then run
Tasks 21 through 27 once on the combined source tree.

The boundary SHA is valid only when those predicates are true. Confirm them
from the live plan and branch history before recording it:

```bash
PLATFORM_WORKTREE=/home/john/elspeth/.worktrees/deferred-platform-completion
test -z "$(git -C "$PLATFORM_WORKTREE" status --short)"
PLATFORM_IMPLEMENTATION_SHA=$(git -C "$PLATFORM_WORKTREE" rev-parse HEAD)
git -C "$PLATFORM_WORKTREE" show --no-patch --format=fuller \
  "$PLATFORM_IMPLEMENTATION_SHA"
```

This is the preferred autonomy lane because corpus changes enter before the
candidate review, source freeze, immutable image build, live Azure Container
Apps (ACA) and Azure Files acceptance, and receipt binding.

### Lane B — invalidate and repeat post-freeze acceptance

Use this lane if Task 21 or any later candidate-acceptance step has begun. It
is mandatory when the platform task has already completed Task 27 before
corpus integration.

If Task 27 is complete, capture its verified `release/0.7.2` merge commit as
the historical platform base. Branch from that commit, integrate Wave A and
all intended Wave B packets, and explicitly invalidate the prior frozen
candidate, immutable image, live acceptance, and receipt. The prior artifacts
remain historical platform evidence only; they do not accept the changed
combined source tree.

Restart at Task 21 on the combined branch and rerun the full active-plan
sequence:

1. Task 21 independent review, repair, and complete candidate gates.
2. Task 22 source freeze and a new immutable image build.
3. Task 23 live ACA and Azure Files acceptance for that new image.
4. Task 24 receipt rebinding and claim promotion from the new evidence.
5. Task 25 post-binding verifier and complete gates.
6. Task 26 final independent review; any bound-input repair returns to Task 21.
7. Task 27 merge and post-merge release gates on the combined tree.

Local corpus tests, a Docker smoke, or an operator P0 pass cannot restore the
invalidated provider acceptance by themselves.

## What Wave A delivered

### Commit ledger

| Commit | Result |
| --- | --- |
| `fb93fedfe2ae4f53efea56058360eb18236659c9` | Defines the bounded Wave A design and implementation plan. |
| `3d5b793ef6107c76ad66f45ecfee9bd58a36a31a` | Adds the first runtime-consumed input attribution repair. |
| `5e799c06605ae8ddcd896a338189b5669672bc06` | Rejects absent, comment-only, decoy, and mismatched source fixture paths. |
| `90e218f2bbe117e69157e4cd70238f238d179bf5` | Contains decoded repository-relative documentation links inside the repository. |
| `0e45845cb4a0f0e7e0a14a87619c4330f57e507a` | Adds the typed build workflow and four bounded production-build cases. |
| `65613e479ca56d8c022434d2fe0f7a30f52bd5ec` | Clarifies that build evidence stops before runtime, audit, and recovery. |
| `be5f183aa7735adb3b5e3291709855056b54a68f` | Closes schema/loader evidence-integrity findings from independent review. |
| `f6920ea9a43e99953a9b5e9efa7dec463f1851a3` | Requires harness evidence to stay within its locator scenario and validated lifecycle stages, including rejection from non-lifecycle cells. |

The manifest now has 57 evidence records: 51 `pytest` and 6 `harness`.
It registers six executable cases: four `build`, one `run`, and one
`recovery`.

### Exact Wave A graph facts

| Case | Nodes | Edges | Typed node counts | Sorted edge labels |
| --- | ---: | ---: | --- | --- |
| `multiple-independent-sources:independent-roots` | 3 | 2 | `source=2`, `sink=1` | `on_success`, `on_success` |
| `multi-source-queue-fan-in:queued-fan-in` | 5 | 4 | `source=2`, `queue=1`, `transform=1`, `sink=1` | `continue`, `continue`, `continue`, `on_success` |
| `conditional-routing:two-way-gate` | 4 | 3 | `source=1`, `gate=1`, `sink=2` | `continue`, `false`, `true` |
| `fork-coalesce-policies:require-all-nested` | 4 | 4 | `source=1`, `gate=1`, `coalesce=1`, `sink=1` | `continue`, `on_success`, `path_a`, `path_b` |

Each case crosses real YAML loading, plugin instantiation in preflight mode,
`ExecutionGraph.from_plugin_instances`, graph and edge validation, and
production pipeline-config assembly. The declared expectation and observed
evidence require the same exact node count, edge count, node-type counts, and
sorted labels. Duplicate edge labels remain significant.

### Evidence boundary

Wave A proves:

- strict manifest, fixture, evidence-locator, and documentation-link contracts;
- the declared input fixture is the input every configured source consumes;
- production configuration loading and plugin preflight instantiation;
- production graph construction and validation;
- production pipeline-config assembly; and
- exact build graph evidence for the four cases above.

Wave A does **not** prove:

- Orchestrator traversal or output disposition for those four build cases;
- Landscape audit records or audit-first ordering;
- restart, checkpoint, resume, redrive, or crash recovery;
- lease expiry, reclaim, late-completion refusal, or multi-worker races;
- guided authoring or semantic round-trip completeness; or
- release, Docker, Kubernetes, Azure, trust-tier, or fingerprint acceptance.

The four build cases deliberately report only completed stages `config` and
`build`. Runtime, audit, and recovery remain explicit unattempted zero-value
evidence. Do not promote later cells by analogy.

## Live remaining-cell ledger

The current manifest has 15 scenarios and 165 cells. Exactly 44 are `pass`,
55 `partial`, 16 `fail`, 38 `unknown`, and 12 `not_applicable`. Excluding
`pass` and `not_applicable`, 109 applicable cells remain.

| Dimension | Pass | Partial | Fail | Unknown | N/A | Applicable non-pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `config` | 13 | 0 | 1 | 0 | 1 | 1 |
| `build` | 13 | 1 | 1 | 0 | 0 | 2 |
| `contracts` | 6 | 8 | 1 | 0 | 0 | 9 |
| `runtime` | 0 | 12 | 1 | 2 | 0 | 15 |
| `audit` | 0 | 12 | 0 | 2 | 1 | 14 |
| `recovery` | 0 | 8 | 0 | 6 | 1 | 14 |
| `concurrency` | 0 | 4 | 0 | 10 | 1 | 14 |
| `freeform` | 12 | 0 | 1 | 0 | 2 | 1 |
| `guided` | 0 | 2 | 11 | 0 | 2 | 13 |
| `round_trip` | 0 | 7 | 0 | 5 | 3 | 12 |
| `scale` | 0 | 1 | 0 | 13 | 1 | 14 |
| **Total** | **44** | **55** | **16** | **38** | **12** | **109** |

Current ownership encoded in those 109 cells:

| Owner issue | Cells | Guidance |
| --- | ---: | --- |
| `elspeth-ef29ef6ba4` | 78 | Keep as the corpus umbrella; create bounded children rather than one long-lived implementation claim. |
| `elspeth-7cf763da7c` | 12 | Playwright semantic round-trip lane; currently open and unclaimed. Reconcile before creating a corpus child. |
| `elspeth-7e2dd67275` | 12 | Guided gate capability; currently held by John Morrissey. Do not steal or duplicate it. |
| `elspeth-a5b86149d4` | 6 | Row-union product capability; currently held by John Morrissey. Prove current rejection without inventing the capability. |
| `elspeth-7cdc4da434` | 1 | Closed state-engine issue still owns `row-expansion-parent-child-recovery.recovery:partial`; reconcile that stale owner before promoting or reassigning the cell. |

## Independently executable packets

The packets partition all 109 applicable non-pass cells exactly. A packet is a
test/evidence boundary, not permission to claim the external capability issue
named by a cell.

| Packet | Scenarios and dimensions | Cells | Platform posture |
| --- | --- | ---: | --- |
| B1 — source, fan-in, and routing runtime | `linear`, `multiple-independent-sources`, `multi-source-queue-fan-in`, `conditional-routing`; runtime/audit/recovery/concurrency/scale | 20 | Start from Lane A's verified implementation boundary or Lane B's explicitly invalidated prior-acceptance base. |
| B2 — fork, coalesce, and parallel runtime | `fork-multiple-terminals-partial-failure`, `fork-coalesce-policies`, `sequential-nested-fork-coalesce`, `parallel-coalesces`; remaining build/contracts plus runtime/audit/recovery/concurrency/scale | 24 | Start from Lane A's verified implementation boundary or Lane B's explicitly invalidated prior-acceptance base. |
| B3 — stateful data and disposition runtime | `aggregation-immutable-batch`, `row-expansion-parent-child-recovery`, `retry-quarantine-discard-routed-errors`, `sink-write-pending-redrive`; contracts/runtime/audit/recovery/concurrency/scale | 23 | Use the selected lane's base; rehome the stale closed owner before changing row-expansion recovery. |
| B4 — checkpoint and distributed ownership | `checkpoint-deterministic-resume`, `multi-worker-lease-reclaim-late-completion`; contracts/runtime/audit/recovery/concurrency/scale | 12 | Most platform-coupled packet. Reuse the active plan's landed session/run/Landscape authority and full two-process harness; do not build a parallel lease model. |
| C1 — authoring and semantic round-trip | Guided and `round_trip` cells for the 12 applicable scenarios from `linear` through `sink-write-pending-redrive`, excluding `row-union-interleave` | 24 | Independent of platform runtime, but gated by live ownership and product behavior in `elspeth-7e2dd67275` and `elspeth-7cf763da7c`. |
| C2 — row-union decision boundary | `row-union-interleave` config/build/contracts/runtime/freeform/guided | 6 | Independent product lane. Until `elspeth-a5b86149d4` lands, add executable fail-closed rejection evidence and retain honest `fail` statuses. |

B1 through B4 may run in parallel only in isolated worktrees with distinct
fixture directories and packet-specific test modules. They all need the
manifest, loader, and common harness, so one coordinator must integrate them
serially and rerun the full corpus gate after every packet. C1 and C2 can
remain independent of the platform trigger, but integrating them after the
Wave A rebase avoids a second collision round.

## Rebase and integration order

- [ ] **Step 1: Refresh live state and plan authority.** Read the parent, all
  cell-owner issues, deferred-platform issues, release task, Docker task, and
  operator P0. Check active worktrees and claims. Read the full plan from the
  active platform worktree. A stale lease is not permission to steal a
  human-owned or advancing task.
- [ ] **Step 2: Choose Lane A or Lane B.** Use Lane A only while Tasks 1 through
  20 are complete and Task 21 has not begun. If Task 21 has begun, any corpus
  source/evidence integration uses Lane B and invalidates current candidate
  acceptance.

### Lane A procedure — integrate before Task 21

- [ ] **A1: Capture the clean implementation boundary.** Keep the platform
  worktree paused after Task 20 and set `PLATFORM_IMPLEMENTATION_SHA` with the
  Lane A command above. Record why the live plan's implementation and full
  two-process gates are complete at that commit.
- [ ] **A2: Rebase Wave A onto that boundary.** In one shell:

  ```bash
  PLATFORM_WORKTREE=/home/john/elspeth/.worktrees/deferred-platform-completion
  WAVE_A_WORKTREE=/home/john/elspeth/.worktrees/dag-corpus-wave-a
  PLATFORM_IMPLEMENTATION_SHA=$(git -C "$PLATFORM_WORKTREE" rev-parse HEAD)
  test -z "$(git -C "$PLATFORM_WORKTREE" status --short)"
  test -z "$(git -C "$WAVE_A_WORKTREE" status --short)"
  git -C "$WAVE_A_WORKTREE" rebase --onto \
    "$PLATFORM_IMPLEMENTATION_SHA" \
    2a3452e3452581008b5fb61b1e75ad0b8f03fb2f \
    codex/dag-corpus-wave-a
  ```

  Do not resolve conflicts by choosing one whole side. Keep the platform
  runtime/session/Landscape authority and reapply the Wave A typed
  manifest/schema/harness contracts around that production path. The rebase
  must bring in the active 27-task plan; reread that merged copy before
  continuing.
- [ ] **A3: Reverify Wave A and recompute the ledger.** The focused gate must
  collect the same 191 tests unless an intentional reviewed packet changes the
  inventory. Static checks and `git diff --check` must pass.
- [ ] **A4: Execute and integrate the packets.** Create one child issue and
  isolated branch per packet. Base every packet on the same verified
  platform-plus-Wave-A tip. Integrate B1, B2, B3, B4, C1, then C2 serially into
  `codex/dag-corpus-wave-a`, rerunning the full corpus gate after each packet.
  Recompute the manifest ledger in the same commit as each evidence/status
  change.
- [ ] **A5: Merge the complete corpus into the paused platform branch.** With
  both worktrees clean:

  ```bash
  PLATFORM_WORKTREE=/home/john/elspeth/.worktrees/deferred-platform-completion
  git -C "$PLATFORM_WORKTREE" merge --no-ff codex/dag-corpus-wave-a \
    -m "merge: integrate DAG corpus before candidate freeze"
  git -C "$PLATFORM_WORKTREE" status --short --branch
  ```

  Rerun the corpus and implementation gates on this combined branch. This
  merge is before Task 21, so no candidate image or receipt exists to preserve.
- [ ] **A6: Execute Tasks 21 through 27 once on the combined branch.** Reread
  the active plan from the platform worktree, then perform independent review,
  freeze, immutable build, live provider acceptance, receipt binding,
  post-binding verification, final review, merge, and post-merge release gates
  in their documented order.

### Lane B procedure — invalidate and repeat acceptance

- [ ] **B1: Capture the accepted platform base.** If Task 27 already
  completed, require a clean `release/0.7.2` worktree and record its full SHA:

  ```bash
  RELEASE_WORKTREE=/home/john/elspeth
  test "$(git -C "$RELEASE_WORKTREE" branch --show-current)" = "release/0.7.2"
  test -z "$(git -C "$RELEASE_WORKTREE" status --short)"
  PRIOR_ACCEPTANCE_BASE_SHA=$(git -C "$RELEASE_WORKTREE" \
    rev-parse release/0.7.2^{commit})
  git -C "$RELEASE_WORKTREE" show --no-patch --format=fuller \
    "$PRIOR_ACCEPTANCE_BASE_SHA"
  ```

  If Task 21 has begun but Task 27 has not completed, use the clean current
  candidate branch commit instead and assign its full commit ID to
  `PRIOR_ACCEPTANCE_BASE_SHA`. In either case, call it a prior acceptance base,
  not the final combined release.
- [ ] **B2: Rebase Wave A and execute all intended packets.** Rebase the clean
  Wave A branch onto the prior acceptance base using the same `rebase --onto`
  shape as Lane A, with `PRIOR_ACCEPTANCE_BASE_SHA` as the new base. Reread
  the now-current merged 27-task plan. Reverify 191 tests, then integrate B1
  through C2 serially and recompute the ledger after each packet.
- [ ] **B3: Record invalidation before making release claims.** The corpus
  diff changes source/evidence paths outside Task 24's closed post-acceptance
  allowlist. Preserve the earlier image and receipt as historical evidence,
  but do not reuse their accepted status, hashes, or provider claims for the
  combined branch.
- [ ] **B4: Create a fresh combined acceptance branch/worktree.** For example:

  ```bash
  RELEASE_WORKTREE=/home/john/elspeth
  COMBINED_BRANCH=codex/dag-corpus-post-platform-acceptance
  COMBINED_WORKTREE=/home/john/elspeth/.worktrees/dag-corpus-post-platform-acceptance
  git -C "$RELEASE_WORKTREE" branch "$COMBINED_BRANCH" \
    codex/dag-corpus-wave-a
  git -C "$RELEASE_WORKTREE" worktree add \
    "$COMBINED_WORKTREE" "$COMBINED_BRANCH"
  sed -n '1,$p' \
    "$COMBINED_WORKTREE/docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md"
  ```

  Use a different explicit branch/worktree name if either already exists; do
  not overwrite or reuse an active worktree.
- [ ] **B5: Restart at Task 21.** Run full independent review and candidate
  gates, freeze the combined source, build a new immutable image, repeat live
  ACA and Azure Files acceptance, bind a new receipt, run the post-binding
  verifier, and conduct final independent review. Any bound-input repair
  invalidates the new evidence and returns execution to Task 21.
- [ ] **B6: Run Task 27 for the new candidate.** Merge the actual
  `COMBINED_BRANCH` rather than the old completed platform branch, then run the
  binding verifier and both post-merge release-gate modes on `release/0.7.2`.
  Only this new combined acceptance can become final release evidence.

## Collision and coordination map

| Surface | Collision rule |
| --- | --- |
| `tests/fixtures/dag_scenario_corpus/harness.py` | One packet at a time may change common dispatch or evidence shapes. Prefer packet-specific adapters before expanding the shared harness. |
| `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` | Coordinator-owned during integration. Workers may prepare packet-local manifest patches, but the coordinator reapplies and validates them serially. |
| Orchestrator | Use the landed production entry point and run-admission authority. Never add a corpus-only execution shortcut or bypass a permit/fence to make evidence pass. |
| Landscape | Use the landed audit-first, run-ownership, checkpoint, and finalization contracts. Every late or stale worker assertion must prove refusal before durable mutation. |
| Sessions and blob authority | Do not reuse pre-platform process-local locks. Resolve session operation fences, blob version domains, and cleanup authority from the merged platform implementation. |
| Checkpoint/recovery | Share production checkpoint compatibility and resume admission. Do not mock away implementation identity, sink-effect safety, or incomplete-source checks. |
| Multi-worker/lease/concurrency | Reuse the active plan's verified independent-process PostgreSQL harness and exact lease/epoch semantics. A thread-only or single-process imitation is not evidence. |
| Guided/Playwright authoring | Coordinate with the current owners of `elspeth-7e2dd67275` and `elspeth-7cf763da7c`; do not edit their active worktrees or convert product gaps into corpus-only behavior. |
| Row union | Preserve the tested unsupported/rejection contract until the product issue lands. Queue and coalesce are not substitutes for a row-union barrier. |
| CI and release files | Packet workers do not edit version, Docker, deployment, trust-tier, fingerprint, or release-gate surfaces. Escalate any required gate change to the release coordinator. |

## Packet execution discipline

For each packet:

1. Refresh the parent and external owner issues. Create a child under
   `elspeth-ef29ef6ba4` with exact scenarios, dimensions, files, and done
   definition.
2. Atomically use `start-work --advance` on that child with a packet-specific
   assignee. On `CONFLICT`, stop; do not clear another assignee.
3. Resolve current production owners through Loomweave before broad source
   navigation. Use Warpline on the completed diff to derive the re-verification
   worklist; unavailable enrichment is not a clean result.
4. Write the failing production-path assertion first. A skip, xfail, direct
   graph construction, documentary reference, or plan citation is not
   executable corpus evidence.
5. Implement the smallest complete fixture/harness change. Defects discovered
   in the packet's correctness boundary belong to the packet; do not hide them
   in an expiring observation.
6. Obtain fresh specification-compliance and quality reviews. Repair every
   valid finding and rerun the exact regression plus the full corpus gate.
7. Commit the packet, add exact verification and commit evidence to its child,
   close only that child, and release any umbrella claim. Keep the parent open
   until the complete manifest acceptance is actually satisfied.

## Verification commands

Run these from the combined corpus worktree after the rebase and after every
packet integration:

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

For B4, also rerun the exact per-file and repeated combined PostgreSQL
multi-instance commands from the live deferred-platform plan. In the active
27-task plan this is the full two-process failure/cancellation/race task; do not
substitute the differently numbered task in the stale Wave A plan copy. Copy
the commands from the current combined worktree after integration rather than
preserving a duplicate here. Run every additional test Warpline identifies.
Before final release closeout, run the repository-owned local release gates in
both agent and operator modes on the combined tree.

## Final release, Docker, and P0 rule

Platform-only or corpus-only proof is not final release proof.

- In Lane A, Tasks 21 through 27 run once after corpus integration.
- In Lane B, corpus integration invalidates any earlier Task 21 through 27
  result. Repeat Tasks 21 through 26 on the combined branch and Task 27 on the
  new merge. Do not try to repair acceptance with a narrow closeout.

The final combined acceptance must include all of these facts:

1. Task 21 independently reviews the complete platform-plus-corpus tree and
   runs both release-gate modes. Resolve every source/evidence repair before
   the candidate source freeze.
2. The operator P0 trust-tier signing and supported fingerprint-baseline
   regeneration apply to that exact combined candidate tree. If the P0 ran for
   a platform-only tree, its result is historical and must be repeated. Never
   hand-edit the baseline, bypass signature verification, or repurpose operator
   credentials.
3. Task 22 freezes that reviewed source and builds a new immutable image. Its
   Docker checks must reconfirm startup, readiness, runtime UID/filesystem
   behavior, and the documented external PostgreSQL/web contract. An earlier
   RC or local Docker smoke cannot certify the changed image.
4. Tasks 23 through 26 repeat live ACA and Azure Files acceptance, bind a new
   receipt, run the post-binding verifier, and complete final independent
   review. Local Docker and P0 success do not replace these provider facts.
5. Task 27 produces the final combined `release/0.7.2` merge SHA and reruns the
   binding verifier plus both release-gate modes on that merge. Record the full
   SHA only after those post-merge checks pass. Any later source, test,
   manifest, image-input, trust-tier, or receipt-input change invalidates the
   result and returns execution to Task 21.

Do not close release/Docker work on platform-only evidence. If those issues
were already closed by a completed platform-only Task 27, record new combined
acceptance work under the live tracker workflow before calling the release
final. Keep `elspeth-ef29ef6ba4` open until every applicable manifest cell is
executable and passing, or an unsupported capability has an explicit,
executable, fail-closed rejection contract with live ownership for the
remaining product work.

## Stop conditions

Stop the affected packet and return to coordination when any of these occurs:

- the worker has not read the active 27-task plan from the live or combined
  worktree;
- Lane A's Task 1-through-20 boundary is not clean and fully verified, or Task
  21 has already begun without switching to Lane B;
- a captured implementation or prior-acceptance SHA changes during packet
  setup;
- someone proposes a corpus or other source/evidence edit after Task 21 while
  retaining the existing image, live provider acceptance, or receipt;
- Lane B does not schedule the complete Task 21-through-27 repeat after
  invalidation;
- a worker encounters an active claim or human-owned capability issue;
- a production path requires bypassing a session, run, Landscape, checkpoint,
  lease, or signature authority;
- a manifest status would be promoted without complete executable evidence;
- the full corpus collection shrinks unexpectedly or introduces a skip/xfail;
  or
- any source change occurs after the final release SHA is accepted without
  returning to Task 21.
