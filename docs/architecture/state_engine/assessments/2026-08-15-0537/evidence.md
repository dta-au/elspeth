# Verification Runs — 2026-08-15-0537

All evidence was captured at frozen commit
`2b4b04a8a852a839b7b395b0bcdfceb95676606b` in the dedicated worktree
`/home/john/elspeth/.claude/worktrees/state-engine-v3-assessment`
(branch `codex/state-engine-v3-assessment`), with a hermetic venv built by
`uv venv .venv --python 3.13 && uv sync --frozen --all-extras` and the
in-tree import verified (`elspeth.__file__` under the worktree `src/`)
before every run. Python 3.13 is the release interpreter for this branch
(`ci.yaml` maintains 3.12/3.13); the venv was rebuilt from 3.14 after the
first capture attempt showed 3.14-only failures.

## Freeze declaration

Freeze F is `2b4b04a8a`, three commits above the campaign merge
`0dfaa407a` on the release branch. The deltas, each a required Step-0
executable/configuration change committed before the freeze:

1. `6b44f36c6` — large-file hook exclusion for v3 assessment manifests.
2. `368b8734f` — profile reporter accepts agreeing multi-probe
   observations (first probe binds `deployment_probe`; disagreement still
   fail-closes), with three pinned unit tests. Discovered by the first
   full-lane single-invocation run; per-cohort runs never co-collected
   two probes.
3. `2b4b04a8a` — `test_state_engine_supported_profiles.py` names v3 as
   the maintained-current catalog (explicit PostgreSQL 16) with a
   separate frozen-v2 immutable-history assertion, as the plan required
   before this freeze.

Adjudication of the release-tip delta at freeze time: since `0dfaa407a`
the release branch had moved only in web front-end surfaces plus one
additive Landscape index (`ix_token_outcomes_run_token`, epoch 32→33,
`elspeth-c675c8c2d9`); `docs/architecture/state_engine/`,
`tests/unit/architecture/`, and `.github/workflows/` were byte-identical,
and the v3 catalog does not pin the schema epoch, so the assessed
surfaces equal the tip's.

## Lane runs (retained, reporter-bound)

Each lane ran serially and uncontended with the exact node list from the
committed selector manifest, `-n 0`, the trusted profile reporter loaded
exactly once, and JUnit/profile/stdout/stderr retained under `evidence/`
with digests bound in `assessment.json`. Safe environment:
`PYTHONHASHSEED=0`, `PYTHONPATH=<worktree>/src`,
`TZ=Australia/Canberra`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`.

| Evidence ID | Lane | Nodes | Result | Duration | Cells promoted |
| --- | --- | ---: | --- | ---: | ---: |
| EV-LOCAL-SINGLE-PROCESS | local-sqlite-wal-single-process-leader | 712 | all passed, 0 skips | 438.0s | 440 |
| EV-LOCAL-SAME-HOST-FOLLOWERS | local-sqlite-wal-same-host-leader-plus-claim-only-followers | 30 | all passed, 0 skips | 193.1s | 39 |
| EV-LOCAL-WEB-CLI-FOLLOWERS | local-sqlite-wal-web-hosted-leader-plus-same-host-cli-followers | 3 | all passed, 0 skips | 207.3s | 10 |

The exact argv of each run (including every node ID) is recorded in its
evidence record. The profile reporter observed
`sqlite-wal-single-process-leader`,
`sqlite-wal-same-host-leader-plus-claim-only-followers`, and
`sqlite-wal-web-hosted-leader-plus-same-host-cli-followers` respectively
from trusted runtime connection probes; node identities agree across
JUnit, profile report, and the retained node index for every lane.

## Coverage mapping provenance

Local-lane coverage is authored at assessment time from the retained
runs, per the selector-manifest contract. The node-to-cell mapping was
produced in nine reviewed passes over the actual test sources (one per
test-module group), each recording per-node leg, case, evidenced
dimensions, confidence, and a one-line assertion-basis rationale, then
merged under these mechanical rules: every node maps to at most one
`(leg, case, profile)` subject; a cell is claimed only when at least one
passing node's assertions evidence that dimension; cells outside the
lane's manifest partition are rejected. 634 of the 745 lane nodes are
cited by at least one coverage cell (601 single-process + 30 follower +
3 web-CLI); the remaining 111 — all in the single-process lane — are
deliberately unclaimed (cross-cutting schema/inventory pins, instrument
self-tests, AST source-inspection gates, multi-subject guards,
circular lockstep parity guards, web-engine accounting nodes outside
the claim boundary, plugin-config validation nodes, and tests whose
assertions prove no catalog cell). These figures are recomputable from
`assessment.json`'s coverage arrays against the retained node indexes.

The full per-node mapping record — subject, evidenced dimensions,
reviewer confidence, and assertion-basis rationale for every cited node,
plus every unclaimed node's exclusion reason — is retained in this
package as `coverage-mapping.json`, so a future agent can audit the
mapping decisions without reconstructing them. `assessment.json`'s
coverage arrays remain the machine authority; the mapping record is
deliberately not digest-bound in the manifest (it is fully recomputable
against the coverage arrays, and the publication commit binds its
bytes), unlike the twelve retained lane artifacts, which are.

PB-09 lifecycle mapping is mechanical from the matrix parametrization
(34 local plugins × five phases), with the phase-to-dimension
adjudication read directly from the five test bodies:

- base lifecycle crossing → `production_entry`, `success_effects`,
  `boundary_composition` (real `Orchestrator.run` through the public
  boundary; durable node/outcome/effect/artifact images asserted);
- partial-start failure → `boundary_composition` (the ordered
  no-cleanup-of-the-unstarted-subject contract; independent review
  showed the earlier zero-mutation reading was tautological under the
  monkeypatched start and checked no durable plane, so those 34 cells
  are not claimed);
- partial-cleanup and exceptional teardown → `boundary_composition`
  (ordered unwind of exactly the started subset);
- public resume after a finalized-effect response loss →
  `crash_restart` (fresh objects and reopened store converge; exactly
  one finalized effect; checkpoint cleared).

## Central adjudications a reviewer should weigh

- **Profile binding of repository-direct nodes.** Some lane-1 nodes
  exercise repository surfaces on in-memory SQLite
  (`journal_mode=memory`) rather than a WAL file; the committed selector
  manifest assigns them to the single-process WAL lane, whose profile
  claim is carried by the lane's trusted runtime probe plus its
  file-backed e2e/process nodes. The checkable invariant (as corrected
  by the fresh re-reader, who falsified the first version): no
  `concurrency` or `crash_restart` cell is promoted from an
  in-memory-only node; in-memory-only cells are otherwise confined to
  `precondition_image`, `read_model_truth_table`,
  `zero_mutation_rollback`, `guard_refusal`, `production_entry`, and
  `maintenance`, with two named exceptions — TS-17 and TS-18
  `success_effects`, whose exact-image assertions run on an in-memory
  repository store reached via a raw `create_engine("sqlite:///:memory:")`
  (the transition-family text does not require a file-backed store).
  Thirteen read-model consumer cells additionally rest on seam-contract
  stand-ins with no store at all, as the read_model family text permits. The concurrency-bearing repository nodes are NOT
  in-memory: both the lease-race and run-coordination modules build two
  independent engines onto one file-backed WAL database through the
  production pragma verification.
- **Dimension discipline.** `concurrency` is cited only where
  independent connections or processes actually contend. `crash_restart`
  is accepted in exactly two shapes, and the catalog's per-family
  acceptance text decides their status: (a) a real process kill with a
  fresh-process re-attach (20 cells promoted `pass`, concentrated in
  the e2e process-death and lifecycle-process matrices, plus the three
  transition/forbidden cells — TS-02, TS-05, F-07 via the
  concurrent-resume harness — whose family text accepts independent
  connections or a fresh object); (b) a durable-store crash
  seam recovered by fresh objects over a reopened store through the
  public `RecoveryManager`/`Orchestrator.resume` boundary — genuine
  durable evidence, but short of `production_boundary`'s fresh-process
  clause, so the 36 such production_boundary cells (33 PB-09 resume
  phases + 3 PB-06 seams) are recorded `partial` with the shortfall and
  exit gate named per cell. Pure in-process exception handling with no
  store reopen was never accepted.
  `maintenance` is claimed by exactly two cells (F-10 and F-12 at the
  single-process profile): the forbidden family's maintenance acceptance
  is the retained deliberate-absence decision, and the citing nodes
  assert precisely that (the removed optional-authority helper stays
  removed and unreferenced). One of those nodes performs source-text
  containment inspection; it is retained because the forbidden-family
  acceptance for this dimension is a deliberate-absence check, unlike
  the six unclaimed AST gates whose intended legs have behavioral
  acceptance.
- **Dual-leg nodes.** A handful of nodes genuinely evidence two legs;
  the one-node-one-subject rule binds each to its primary leg and the
  secondary evidence is deliberately dropped rather than double-counted.

## Trust and boundary gates at F

- `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack
  scripts.wardline_pack --allow-custom-packs --local-only`: exit 1 with
  exactly the six pre-existing `PY-WL-102` composer findings
  (`elspeth-5a322bd5ca`), 129 recognized trust boundaries, non-inert.
  `git diff 0dfaa407a..F -- src/` is empty, so the campaign delta is
  zero by construction.
- Trust-tier lint (`elspeth-lints check --rules all --root src/elspeth`,
  shape-only key-free mode): the standing fail-closed corpus
  (`elspeth-13f0cc04fb`); identical to the merge-qualified corpus for
  the same reason (`src/` unchanged). Signing remains an operator action
  at package completion; no signatures were touched.
- Full CI-equivalent suite (`pytest tests/ -n 12`, default selection) at
  F, run from the assessment worktree with the in-tree import verified:
  **40382 passed, 79 skipped, 1 xfailed in 1426.00s, `PYTEST_EXIT=0`**
  (in-log exit capture; started 2026-08-15T06:02:37+10:00, ended
  06:26:29). The three lane runs are exact-node-list runs and cannot
  speak to the whole tree; this suite run is the whole-tree freeze
  qualification.

## Reproduction preconditions (normative for reruns)

- **Rewrite the output-transport arguments before replaying any recorded
  argv.** Each record's argv names its retained `--junitxml` and
  `--state-engine-profile-report` paths, so a verbatim replay overwrites
  the very evidence it is meant to verify. Rerun with throwaway output
  paths outside the repository. The capture worktree's `evidence/` files
  are chmod-protected as a local backstop, but git records only plain
  file modes, so a fresh clone's copies are writable — the
  throwaway-paths rule is the real protection. This hazard was
  demonstrated during review.
- **Compare a rerun to the retained `profile.json`, not the JUnit
  bytes.** The JUnit carries `time`/`timestamp`/`hostname` attributes
  and will never byte-match; the profile report's node identities and
  outcomes are the comparison oracle.
- **Know the reporter's silent fail-closed signature.** A rerun that
  prints "N passed" but exits 1 with empty stderr and no profile report
  written means the reporter's session gate fired — no probe observation
  was recorded (typically the probe node was not in your selection), or
  the outcome/node sets mismatched — not that the tree regressed.
  Diagnostic emission for those branches is instrument-hardening work
  tracked as `elspeth-85b7bf2c3d` (companion to `elspeth-d0292e9481`).
- **Run lanes serially on an otherwise quiet host.** The lane runs are
  `-n 0` and were captured uncontended, and that is a required
  precondition, not narrative: the same-host-follower deployment probe
  (`test_same_host_leader_plus_claim_only_followers_profile_covers_sink_effect_process_death_matrix`)
  is contention-sensitive. A review control matrix showed it fails at
  1-minute load average ≈6.5 on this 24-core host (the CLI follower
  observes `no live leader` and exits 1 after the leader's
  heartbeat/lease lapses under load, then the leader times out waiting
  for the pending-sink handoff) and passes in 180s on a quiet host —
  in both co-collected and probe-alone shapes under load. Keep the
  1-minute load average near idle before attributing a probe failure to
  the tree.
- **Reproducibility-class adjudication (dissent preserved).** The
  reviewer argued `deterministic` is the wrong class for a
  contention-sensitive record. The closed class vocabulary has no
  "deterministic under stated preconditions" member; the v2 precedent
  classes pytest lanes `deterministic`; and the divergence mechanism is
  environmental (scheduler starvation), not nondeterminism in the
  contract under test. The class is therefore retained with the
  precondition above published as normative, and the dissent is
  preserved in the review record rather than silently resolved.

## Independent-review remediation

Review 2 (evidence-to-cell validity; 46 pre-registered sampled cells,
including one read-only mutation experiment) found seven material
findings, all accepted:

- 72 cells were demoted from `pass` to `partial` — real retained
  evidence short of the catalog's per-family acceptance text (34 PB-09
  `success_effects` whose assertions were mutation-proven
  non-discriminating; 36 production_boundary `crash_restart` cells
  proven fresh-object rather than fresh-process; RC-02 `concurrency`
  proven with independent connections, not processes; TS-05 follower
  `success_effects` proven via the legacy unfenced adapter). Each
  partial override names its shortfall, owner, and exit gate.
- 41 cells were dropped to `unknown` — the cited nodes did not evidence
  their dimension (the 34 PB-09 partial-start `zero_mutation_rollback`
  cells, whose nodes were re-dimensioned to the `boundary_composition`
  unwind contract they do prove; four read-model `production_entry`
  cells built on stand-in objects; `source:null` `crash_restart`, an
  applicability argument; PB-10 late-arrival `success_effects`, a
  different code path; and the TS-05 follower `precondition_image` cell
  dropped with the legacy-reaper finding, R2-M4). Citation cleanups: 16
  circular parity-lockstep nodes and two BEFORE_EFFECT-seam nodes
  unclaimed.
- The one exception initially preserved (`source:null` `success_effects`,
  on its exact EMPTY-branch equality) was REJECTED by the fresh
  re-reader on two mutation-confirmed grounds: the branch is selected by
  the observed run status, so a row-emitting null source routes to the
  generic branch undetected, and the EMPTY image omits the scheduler
  plane. The cell is demoted to `partial` like its 33 siblings, with its
  own reason and exit gate.

Process incident, recorded for transparency: the first reproducibility
reviewer's rerun overwrote two retained lane-2 artifacts (its own run,
executed with the retained output paths against instructions, recorded a
probe failure; the capture's untouched stdout and meta show 30/30
passed). Lane 2 was re-executed at the frozen commit — 30 passed, exit
0, 193.1s — its artifacts regenerated and digest-bound, and the
evidence directory made read-only. The reviewer's partial output was
discarded and the reproducibility review restarted against the final
overlay.

## Findings filed from this capture

- The multi-probe reporter defect and its fix (`368b8734f`).
- A vacuous RM-05 discriminator test asserting over test-local values
  (calls no production code); left unclaimed and reported for tracker
  filing.
- Six AST source-inspection tests that collect as ordinary pytest nodes
  but are support-only under the evidence hierarchy; unclaimed.
- A follower recovery test that exercises the explicitly named legacy
  unfenced reaper adapter rather than the fenced production reaper;
  mapped with that limitation recorded.

## Limitations

See `assessment.json` `limitations` for the normative list: unexecuted
live lanes (1,780 cells), provider-backed PB-09 cases without local
internal-composition evidence, local dimensions with no mapped
assertions (4,741 local cells unknown in total), the artifact-kind
vocabulary note, the partial-cell discipline, and the Python 3.13
release-interpreter requirement.
