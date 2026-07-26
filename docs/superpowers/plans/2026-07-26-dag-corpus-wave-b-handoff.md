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
multi-worker evidence only after the deferred-platform program has landed and
passed its full two-process matrix. Independent authoring and product-capability
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
- Wave A implementation head: `be5f183aa7735adb3b5e3291709855056b54a68f`
- Focused corpus gate: `186 passed in 23.94s`
- Manifest verdict: `not_complete`
- Parent: `elspeth-ef29ef6ba4`, open and `in_progress`
- Closed Wave A children:
  - `elspeth-e8acea2a55` at
    `5e799c06605ae8ddcd896a338189b5669672bc06`
  - `elspeth-d88d0e45c0` at
    `90e218f2bbe117e69157e4cd70238f238d179bf5`
  - `elspeth-a77a50d44d` at
    `be5f183aa7735adb3b5e3291709855056b54a68f`

The deferred-platform worktree is **not complete**. During this handoff it
advanced within Task 4 review/repair on `codex/deferred-platform-completion` to
a clean committed HEAD
`60eb0da22aec1f0144606f328613a011794aa244`. Neither that commit nor any other
current intermediate platform commit is a Wave B base.

Authoritative inputs:

- [Wave A design](../specs/2026-07-26-dag-corpus-wave-a-design.md)
- [Wave A implementation plan](2026-07-26-dag-corpus-wave-a.md)
- [Deferred-platform controlling plan](2026-07-26-finish-deferred-deployment-platforms.md)
- [Maintained corpus manifest](../../architecture/dag/scenario-corpus/v1/manifest.yaml)
- [DAG completeness criteria](../../architecture/dag/completeness-criteria.md)

## Wave B trigger

Do not guess the platform boundary from a transient branch commit. Wave B's
platform base is the actual `release/0.7.2` commit produced after the current
deferred-platform plan completes all of these gates:

1. Task 13's full two-process failure, cancellation, and race matrix passes
   per file and in two repeated combined runs.
2. Task 21's independent review and complete candidate gates pass; any repair
   is committed and the complete gate reruns.
3. Tasks 22 through 26 complete immutable-image acceptance, receipt binding,
   post-binding verification, and final independent review without stale
   evidence.
4. Task 27 merges the reviewed branch to `release/0.7.2` and the post-merge
   binding verifier plus both release-gate modes pass on the merged tree.

After Task 27, capture the trigger without abbreviating it:

```bash
cd /home/john/elspeth
test "$(git branch --show-current)" = "release/0.7.2"
test -z "$(git status --short)"
PLATFORM_RELEASE_SHA=$(git rev-parse release/0.7.2^{commit})
git show --no-patch --format=fuller "$PLATFORM_RELEASE_SHA"
```

Record `PLATFORM_RELEASE_SHA` in the parent issue before starting a Wave B
child. The Task 13 commit proves a critical runtime boundary, but the current
plan does not integrate it separately; therefore it is not by itself a safe
rebase target.

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
| B1 — source, fan-in, and routing runtime | `linear`, `multiple-independent-sources`, `multi-source-queue-fan-in`, `conditional-routing`; runtime/audit/recovery/concurrency/scale | 20 | Start from the captured platform release SHA. |
| B2 — fork, coalesce, and parallel runtime | `fork-multiple-terminals-partial-failure`, `fork-coalesce-policies`, `sequential-nested-fork-coalesce`, `parallel-coalesces`; remaining build/contracts plus runtime/audit/recovery/concurrency/scale | 24 | Start from the captured platform release SHA. |
| B3 — stateful data and disposition runtime | `aggregation-immutable-batch`, `row-expansion-parent-child-recovery`, `retry-quarantine-discard-routed-errors`, `sink-write-pending-redrive`; contracts/runtime/audit/recovery/concurrency/scale | 23 | Start from the captured platform release SHA; rehome the stale closed owner before changing row-expansion recovery. |
| B4 — checkpoint and distributed ownership | `checkpoint-deterministic-resume`, `multi-worker-lease-reclaim-late-completion`; contracts/runtime/audit/recovery/concurrency/scale | 12 | Most platform-coupled packet. Use the landed session/run/Landscape authority and Task 13 process harness; do not build a parallel lease model. |
| C1 — authoring and semantic round-trip | Guided and `round_trip` cells for the 12 applicable scenarios from `linear` through `sink-write-pending-redrive`, excluding `row-union-interleave` | 24 | Independent of platform runtime, but gated by live ownership and product behavior in `elspeth-7e2dd67275` and `elspeth-7cf763da7c`. |
| C2 — row-union decision boundary | `row-union-interleave` config/build/contracts/runtime/freeform/guided | 6 | Independent product lane. Until `elspeth-a5b86149d4` lands, add executable fail-closed rejection evidence and retain honest `fail` statuses. |

B1 through B4 may run in parallel only in isolated worktrees with distinct
fixture directories and packet-specific test modules. They all need the
manifest, loader, and common harness, so one coordinator must integrate them
serially and rerun the full corpus gate after every packet. C1 and C2 can
remain independent of the platform trigger, but integrating them after the
Wave A rebase avoids a second collision round.

## Rebase and integration order

- [ ] **Step 1: Refresh live state.** Read the parent, all cell-owner issues,
  deferred-platform issues, release task, Docker task, and operator P0. Check
  active worktrees and claims. A stale lease is not permission to steal a
  human-owned or advancing task.
- [ ] **Step 2: Capture the platform release SHA.** Use the post-Task-27 command
  above only after the plan's post-merge gates are green.
- [ ] **Step 3: Detect whether Wave A is already integrated.** Run:

  ```bash
  git -C /home/john/elspeth merge-base --is-ancestor \
    be5f183aa7735adb3b5e3291709855056b54a68f \
    "$PLATFORM_RELEASE_SHA"
  ```

  Exit 0 means the implementation is already in the platform release tree.
  Skip replay and create a new corpus integration branch from that SHA.
- [ ] **Step 4: Otherwise replay Wave A onto the platform release.** In the
  clean Wave A worktree:

  ```bash
  cd /home/john/elspeth/.worktrees/dag-corpus-wave-a
  git rebase --onto "$PLATFORM_RELEASE_SHA" \
    2a3452e3452581008b5fb61b1e75ad0b8f03fb2f \
    codex/dag-corpus-wave-a
  ```

  Do not resolve conflicts by choosing one whole side. Keep the landed
  platform runtime/session/Landscape authority, then reapply the Wave A typed
  manifest/schema/harness contract around that production path. Recompute the
  ledger and graph facts after the rebase.
- [ ] **Step 5: Reverify Wave A.** The focused gate must collect the same 186
  tests unless an intentional reviewed packet changes the inventory. Static
  checks and `git diff --check` must pass.
- [ ] **Step 6: Create one child issue and isolated branch per packet.** Base
  every B packet on the same verified combined platform-plus-Wave-A SHA. Give
  each worker a unique fixture subtree and packet-specific test module.
- [ ] **Step 7: Integrate packet branches serially.** Recommended order is B1,
  B2, B3, B4, C1, C2. Rebase each packet on the current corpus integration tip,
  resolve only its declared files, rerun focused and full corpus gates, then
  comment and close its child. Do not close the parent between packets.
- [ ] **Step 8: Recompute the manifest ledger after every integration.** A cell
  changes status only in the same commit that registers executable evidence
  covering its complete requirement.

## Collision and coordination map

| Surface | Collision rule |
| --- | --- |
| `tests/fixtures/dag_scenario_corpus/harness.py` | One packet at a time may change common dispatch or evidence shapes. Prefer packet-specific adapters before expanding the shared harness. |
| `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` | Coordinator-owned during integration. Workers may prepare packet-local manifest patches, but the coordinator reapplies and validates them serially. |
| Orchestrator | Use the landed production entry point and run-admission authority. Never add a corpus-only execution shortcut or bypass a permit/fence to make evidence pass. |
| Landscape | Use the landed audit-first, run-ownership, checkpoint, and finalization contracts. Every late or stale worker assertion must prove refusal before durable mutation. |
| Sessions and blob authority | Do not reuse pre-platform process-local locks. Resolve session operation fences, blob version domains, and cleanup authority from the merged platform implementation. |
| Checkpoint/recovery | Share production checkpoint compatibility and resume admission. Do not mock away implementation identity, sink-effect safety, or incomplete-source checks. |
| Multi-worker/lease/concurrency | Reuse Task 13's independent-process PostgreSQL harness and exact lease/epoch semantics. A thread-only or single-process imitation is not evidence. |
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

For B4, also rerun the exact Task 13 per-file and twice-combined PostgreSQL
multi-instance commands from the live deferred-platform plan. Copy them from
that plan after integration; do not preserve a stale duplicate here. Run every
additional test Warpline identifies. Before final release closeout, run the
repository-owned local release gates in both agent and operator modes on the
combined tree.

## Final release, Docker, and P0 rule

Platform-only or corpus-only proof is not final release proof. After all
intended platform and corpus packets are integrated:

1. Build and smoke the final Docker image from the combined tree. Reconfirm
   startup, readiness, runtime UID/filesystem behavior, and the documented
   external PostgreSQL/web contract. Earlier RC smoke remains useful history,
   but it cannot certify the final combined image.
2. Run the complete combined corpus, platform, deployment, and local release
   gates. Resolve all source and evidence changes before freezing.
3. Freeze the final release commit and record its full SHA. Do not edit source,
   tests, manifests, trust-tier inputs, or release inputs after this point.
4. Only then ask the operator to perform the authoritative trust-tier signing
   and supported fingerprint-baseline regeneration for that exact frozen SHA.
   Never hand-edit the baseline, bypass signature verification, or repurpose
   operator credentials.
5. If the operator P0 was completed earlier for a platform-only candidate,
   treat that evidence as provisional for the combined release. Re-run the
   operator pass against the final frozen post-platform, post-corpus SHA and
   record the new binding before calling the release gate final.

Keep `elspeth-64c319bf4d` and `elspeth-8d2bea608f` open until their final
combined-tree closeout evidence exists. Keep `elspeth-ef29ef6ba4` open until
every applicable manifest cell is executable and passing, or an unsupported
capability has an explicit, executable, fail-closed rejection contract with
live ownership for the remaining product work.

## Stop conditions

Stop the affected packet and return to coordination when any of these occurs:

- the deferred-platform branch is not merged or its post-merge gates are not
  current;
- the captured platform SHA changes during packet setup;
- a worker encounters an active claim or human-owned capability issue;
- a production path requires bypassing a session, run, Landscape, checkpoint,
  lease, or signature authority;
- a manifest status would be promoted without complete executable evidence;
- the full corpus collection shrinks unexpectedly or introduces a skip/xfail;
  or
- any source change occurs after the final release SHA is frozen.
