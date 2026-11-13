# Handover: composer remediation, audit session to execution session (2026-11-13)

The audit, the plan and the lane briefs are finished and reviewed. This note
holds what the documents do not say: the maintainer's framing, the decisions
still open, and the facts about the machine the work now runs on. Read it
first, then [work-packages.md](work-packages.md).

## State

- Branch `ccr-d14093ba-glu6bw`, pushed. It contains documents, the eval
  harness under `evals/composer-live-audit/` and spike artifacts only. **No
  change under `src/` or `tests/` has been made.** No lane has started.
- Code baseline for every line reference: `main` at `1a7ff0e6`.
- 23 lanes, CDA-01 to CDA-23. 21 are in [lanes/tickets.json](lanes/tickets.json).
  CDA-14 (frontend only) and CDA-16 (investigation) are left out on purpose:
  `lane_manager verify` measures pytest only.
- A Fable reviewer spot-checked the plan against the code three times and
  cleared it each time, with every fix applied
  ([evidence/review/fable-spotcheck.md](evidence/review/fable-spotcheck.md)).
  It did not sample every anchor. Treat a line reference as a pointer and
  confirm it before editing.

## Reading order

1. `AGENTS.md` and `CONTRIBUTING.md` § Whole-tree gates.
2. This note.
3. [work-packages.md](work-packages.md): §2 decisions, §3 waves, §4 rules
   for every lane, §5 briefs, §6 how to run it.
4. [README.md](README.md) for a finding's evidence, when a lane needs it.
5. `spikes/` before CDA-07, CDA-12, CDA-13 and CDA-23. Their instruments and
   prototype patches are in `evals/composer-live-audit/spikes/`.

## The maintainer's framing (given after the plan was written)

- **The goal is a first-class agentic toolkit and composer.** The demo is a
  checkpoint, not the target. All 23 lanes stay, in the existing wave order.
- **The demo is controlled.** The maintainer presents over a Teams call onto
  a big screen, types the prompts, and controls about 90% of the surface.
  Audience questions come in Q&A and can be answered verbally as "still in
  development". It must be an honest demo.
  - The hands-on parts of the runbook in `remediation-plan.md` (16 workers,
    waves of 20, the load rehearsal, login rate limits) do not apply to this
    demo. They stay as product guidance.
  - What matters for the demo is everything visible on the scripted path,
    and the preflight: the exact script run three times on the demo host
    with the real model pair and prompt-guard mode.
- **Latency: the presumption is that the time is needed.** The composer does
  real work (inspection, discovery, validation, advisor review). Examine a
  step only when its time is clearly out of proportion, and then look for
  waste or a bug. A lane that touches timing reports what the step does and
  why it is needed before it proposes shortening it. This sits alongside the
  composer invariants: nothing routes around the provider.

## Suggested landing order

Inside the existing waves, land these first, because they are visible on a
scripted demo path:

1. CDA-02: "what changed" gets a baseline.
2. CDA-07, then CDA-08: advisor capability facts, then the withheld notice
   and receipt presentation.
3. CDA-03, only if the demo script uses the many-rows-to-one-briefing shape.

This is a preference, not a dependency. The wave table still governs which
lanes may run together.

## Open with the maintainer

Ask before the lane that needs the answer; do not guess.

| Item | Needed by | Default written into the brief |
|---|---|---|
| Merge target: this branch, or a new integration branch off `main` | Before wave 1 | None |
| Docker on the execution machine (`docker info` printed nothing on 2026-11-13) | CDA-21, CDA-23 and any lane running the testcontainer suite | None |
| D9a: no-op rule also compares verdict, errors and gates, and requires no repair | CDA-23 | Yes |
| D9b: only the two composer writers skip | CDA-23 | Yes |
| D9c: ship the SPA "No change" outcome (23b); scorer reads a missing repair count as 0 | CDA-23 | Yes to both |
| D3: `report_assemble` framing walk-through, awaiting an explicit OK | CDA-03 | Config-derived framing |
| Which prompts the demo script uses, and whether the tutorial is in it | Runbook preflight | Unknown |

Not yet investigated: how well the screen shows the composer's progress
during a long turn. The backend emits progress events for model calls and
tool batches; nobody has audited how the frontend renders them. If the wait
is honest work, the user should be able to watch it. Worth a small read-only
look and, if needed, a new enhancement lane.

## The execution machine

- 24 CPUs. The `-n 4` and `-n 2` caps in the briefs and tickets were written
  for a 4-CPU cloud box. They may rise, but CPUs do not multiply across
  parallel lanes, and AGENTS.md allows only one broad suite at a time.
- The main checkout is shared and stays on `main`. Never switch it. Work in
  worktrees under `.claude/worktrees/`. The worktree `example-launchers`
  belongs to someone else.
- This branch is checked out in the worktree `composer-desk-audit`, with
  `.venv` linked to the main checkout's environment.
- **Worktree import trap.** The shared environment's editable install points
  at the main checkout. From a worktree, export
  `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src`, pass pytest
  `-o pythonpath=<worktree>/src:<worktree>/elspeth-lints/src` (the
  `pyproject.toml` setting overrides the exported variable), and check
  `elspeth.__file__` and `elspeth_lints.__file__` before trusting a result.
- `OPENROUTER_API_KEY` was not set in the shell on 2026-11-13. Live
  acceptance (the battery re-run, the S1 replay for CDA-07, the A/B for
  CDA-12) needs it. The account held about US$12 at handover.
- The judge-metadata HMAC key must stay out of agent shells.

## What did not come across from the cloud session

- The raw model-call captures (about 580 MB), the test-instance databases
  and the spike worktrees. The measured results are in the spike notes and
  `evidence/`.
- Consequences:
  - `replay.py build` (S1) cannot regenerate request variants; the 44
    prebuilt ones under `S1-advisor-replay/requests/` are enough for
    CDA-07's acceptance.
  - `p10_measure.py` (CDA-23) has no databases to read until a fresh live
    run produces some.
  - The S2 and S3 prototypes exist only as `prototype.patch` files. S3's
    applied cleanly at `58e31760`. S2's is for its wiring only; its overlay
    text is known to regress harder builds.

## Known soft spots

- CDA-12 is the largest and least certain lane (2.5 to 3.5 agent-days). Its
  acceptance turns on five scenarios at three runs per arm. Consider the
  12a/12b split in its brief.
- CDA-23 changes session persistence and must re-pin whole-tree source
  hashes. It runs after CDA-21 and needs the testcontainer suite and the
  full-suite gate.
- The hot-path efficiency items (P7 to P10, E7) come from one reader's code
  inspection. Measure before changing anything.
- The tutorial's Run stage has never been seen to succeed in this audit: the
  cloud container's egress reset every scrape. It must be walked on the demo
  host.
