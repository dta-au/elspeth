# Assessment Review

Review is a technical challenge record, not an approval receipt.

Review outcome: complete

Change the marker to `Review outcome: complete` only after material findings
are resolved or preserved as explicit assessment gaps and a fresh reader has
checked the resulting package.

## Scope

- Assessment ID and baseline: 2026-08-15-0537 at
  `2b4b04a8a852a839b7b395b0bcdfceb95676606b`
- Lens: architecture / evidence / future-agent — three independent readers,
  each followed by a fresh re-reader over the remediated overlay
- Files and evidence inspected: the full prospective publication overlay
  (assessment package plus seven modified hub/authority documents), the v3
  catalog and selector manifest, the retained lane artifacts, and sampled
  test sources (46 pre-registered cells for the evidence lens, including one
  read-only mutation experiment)
- Commands independently rerun: derived-count recomputation from
  `assessment.json`; sha256 of retained artifacts; `collect-evidence`;
  targeted node re-execution with throwaway outputs; a plugin-neutering
  mutation control

## Findings

| ID | Severity | Finding and evidence | Disposition | Changed files or rejection rationale | Re-review result |
| --- | --- | --- | --- | --- | --- |
| R1-M1 | material | evidence.md mapped-node counts stale (660/85 vs artifact truth) | fixed | evidence.md counts recomputed from the artifact (now 634 cited / 111 unclaimed after later demotions) | see Re-review |
| R1-M2 | material | evidence.md falsely claimed maintenance unclaimed; F-10/F-12 claim it soundly | fixed | evidence.md restates the forbidden-family deliberate-absence discipline | see Re-review |
| R1-M3 | material | crash_restart adjudication overclaimed a real-process floor 70% of cells lacked | fixed | evidence.md names the two accepted shapes with exact counts; production_boundary fresh-object cells demoted (R2-M1b) | see Re-review |
| R1-M4 | material | assessment-program.md still named v2 as current (six sites); completeness-criteria.md one site | fixed | both documents updated and added to publication_paths | see Re-review |
| R1-M5 | material | evidence.md forward-referenced a full-suite result that did not yet exist | fixed | actual result recorded: 40382 passed, 79 skipped, 1 xfailed, PYTEST_EXIT=0 at F | see Re-review |
| R2-M1a | material | RC-02 concurrency proved with independent connections, not processes (family text) | demoted | override now `partial` with shortfall, owner, exit gate | see Re-review |
| R2-M1b | material | 36 production_boundary crash_restart cells are fresh-object, not fresh-process | demoted | 33 PB-09 + 3 PB-06 overrides now `partial` | see Re-review |
| R2-M2 | material | 33 PB-09 success_effects cells mutation-proven non-discriminating | demoted | overrides now `partial`; the initially preserved source:null exception was REJECTED on re-review (branch selected by observed status; scheduler plane omitted; mutation-confirmed) and demoted too — final split 417 pass / 72 partial | fresh reader verified all R2 findings applied; ruling recorded |
| R2-M3 | material | 34 PB-09 zero_mutation_rollback cells tautological, no durable plane checked | dropped | cells unknown; nodes re-dimensioned to the boundary_composition unwind contract they do prove | see Re-review |
| R2-M4 | material | TS-05 follower cells rest on the legacy unfenced reaper adapter | demoted/dropped | precondition_image dropped; success_effects `partial` owned by elspeth-c3f3f14c97 | see Re-review |
| R2-M5 | material | four read-model production_entry cells built on stand-in objects | dropped | cells unknown; five nodes lose that dimension | see Re-review |
| R2-M6 | material | source:null crash_restart promoted by a test that never restarts anything | dropped | cell unknown; node unclaimed (applicability is catalog-owned) | see Re-review |
| R2-M7 | material | PB-10 late-arrival success_effects evidenced by a different code path | dropped | cell unknown; node keeps guard_refusal only | see Re-review |
| R2-m1..m7 | minor | negative-claim wording, establishes overclaim, artifact-kind vocabulary, seam misbinding, parity lockstep, in-memory screening | fixed | evidence.md/limitations/does_not_establish updated; 16 parity and 2 seam nodes unclaimed | see Re-review |
| R3-incident | material (process) | first reproducibility reviewer overwrote two retained lane-2 artifacts by replaying the recorded argv verbatim | remediated | lane 2 re-executed at F (30/30, exit 0); artifacts regenerated and digest-bound; evidence directory made read-only; review restarted with a fresh reader | see Re-review |
| R3-M1 | material | collect-evidence lacks intra-record exit_code-vs-counts and JUnit-timestamp-vs-record checks (fault-injection-proven); promotion legality was untestable read-only | validator hardening filed `elspeth-d0292e9481`; injection test run by the lead: a counts mutation is REJECTED (machine-counts agreement), and pass/partial overrides require the all-passed promotable set, so the promotion gate fail-closes at validate-package | no code change inside the freeze; regression test specified in the issue | see Re-review |
| R3-M2 | material | the same-host-follower deployment probe is contention-sensitive (fails at load ≈6.5, passes quiet, both collection shapes); "uncontended" was narrative, not precondition | fixed | evidence.md publishes normative reproduction preconditions with the failure signature; reproducibility-class adjudication recorded with dissent preserved; assessment-program.md rerun steps updated | see Re-review |
| R3-M3 | material | the per-node mapping record (subject, dimensions, confidence, rationale) was outside the package, so provenance figures moved by regeneration rather than auditable correction | fixed | `coverage-mapping.json` retained in the package (634 mapped + 111 unclaimed with reasons and reviewer notes); evidence.md points to it | see Re-review |
| R3-M4 | material | replaying a recorded argv verbatim overwrites the retained evidence it verifies (package hazard for any future re-deriver) | fixed | evidence.md reproduction preconditions + assessment-program.md rerun step 7 mandate throwaway output paths; evidence directory ships read-only | see Re-review |
| RR1-N1 | material | three summary sentences authored pre-demotion never re-derived: derived.reason claimed "every mapped cell passes" (71 are partial); HG-09's parenthetical did not reconcile; the 41-drop enumeration listed 40 | fixed | derived.reason and HG-09 now state the pass/partial split (417/72 after the source:null demotion below); the TS-05 follower precondition_image drop added to the enumeration; slash-list nit corrected; the coverage-mapping digest-binding choice recorded as deliberate | fresh reader verified M1-M5 + minors resolved; N1 fixed in the same cycle |
| RP-M1 | material | the profile reporter's three fail-closed session branches set exit 1 with no diagnostic on any channel (proven: probe-less rerun printed "2 passed", exit 1, empty stderr, no report) | instrument hardening filed `elspeth-85b7bf2c3d`; the silent-exit signature documented in this package's reproduction preconditions and assessment-program §6 so a rerun is diagnosable today | source change deliberately kept outside the freeze (restart cost vs a diagnostics-only emission); interim docs mitigation shipped | verified by fresh clean-room reader |
| RP-M2 | material | R3-M4's rerun warning was placed under the strict-v1 historical section, the package README lacked the cross-reference, and chmod does not survive git (clones ship writable evidence) | fixed | rerun guidance added to assessment-program §6 (current-package context) with the comparison oracle and silent-exit signature; package README §Reproduce now points at the normative preconditions; the chmod claim corrected to a capture-worktree backstop with git-modes caveat | verified by fresh clean-room reader (placement re-fix in same cycle) |
| RR2-N1 | material | the corrected A1 invariant was itself falsified: TS-17/TS-18 success_effects rest solely on a module reaching :memory: via raw create_engine, and 13 read-model consumer cells rest on no-store stand-ins | fixed | the invariant now names both exceptions in evidence.md and the machine record's does_not_establish; TS-17/TS-18 retained on the merits (transition-family text does not require a file-backed store) | prescribed remedy applied verbatim; verified at final inspection |
| RR2-N3 | material | `derived.reason` still read 418 pass / 71 partial against an artifact that is 417/72 — the hand-authored summary drifted a second time when the source:null cell was demoted | fixed | the reviewer's durable fix applied: the assembler now computes the sentence's counts from the override statuses themselves, so it cannot drift from the artifact it summarises; regenerated record reads 417/72 | fresh reader re-ran the full battery on the regenerated record: 417/72 equals an independent recount of the override statuses; nothing else moved |

Findings filed to the tracker from this review cycle: the multi-probe
reporter defect (fixed pre-freeze at `368b8734f`), the vacuous RM-05
discriminator test, the AST-gates-collect-as-pytest hazard, and the
legacy-unfenced-reaper test (`elspeth-c3f3f14c97`).

## Residual limits and dissent

- Unresolved material finding: none accepted as unresolved; demotions and
  drops above are preserved as explicit assessment state (partial/unknown
  cells with owners), not as silent fixes.
- Preserved dissent: the reproducibility reviewer's position that
  `deterministic` is the wrong class for a contention-sensitive record is
  preserved (the class is retained with the reproduction preconditions
  published as normative). The `source:null` exception dissent is closed:
  the fresh re-reader rejected it with a mutation proof and the cell is
  demoted.
- Claims this review did not evaluate: live-lane behavior (unexecuted by
  design; owners named), and the 4,741 local unknown cells outside the
  promoted set.
