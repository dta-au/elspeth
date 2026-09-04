---
name: lane-manager
description: >
  Use when dispatching several subagent lanes from a ticket list and you must
  know — from git and test evidence, not from what a lane says — which lanes
  landed, which are blocked, and what order to merge them in. Triggers: "fan
  out N tickets", "run these as lanes", a lane reports "done", an idle
  notification arrives for a lane, or a lane has gone quiet and you are
  deciding whether to re-dispatch. Also use when recovering a previous run from
  a state file under .claude/lanes/. Harness-neutral; Claude Code users also
  read claude-code.md in this directory.
user-invocable: true
---

# lane-manager — dispatch lanes, verify from evidence, escalate, report

**A lane's completion message is a claim, not a result.** The only things that
count are: commits on the expected branch, the expected files in that diff, and
the lane's test command exiting 0 — all measured by you.

This file is the **harness-neutral procedure**. It assumes only that your
harness gives you three primitives, whatever they are called:

| Primitive | Meaning |
|-----------|---------|
| **spawn**(brief, name) | start a worker agent on a brief, addressable by a name you choose |
| **list-live**() | enumerate the worker agents your harness still considers running |
| **message**(name, text) | send text to a running worker |

Everything else — state, verification, the idle verdict, the escalation
ladder, the report — is the CLI `python .agents/skills/lane-manager/lane_manager.py`
(`--help` for flags), which is plain Python + git and is the contract. The
state file is documented JSON under `.claude/lanes/<run-id>.json`; any agent,
script, or human can read or resume it.

**Running in Claude Code?** Read `claude-code.md` next to this file — it maps
the three primitives to concrete tools and adds harness-specific handling.
Other harnesses: substitute your own equivalents; the CLI does not care.

Worktree discipline from AGENTS.md applies to every lane (one worktree per
lane; `.venv` and `PYTHONPATH` rules). Brief each lane with an explicit test
parallelism ceiling — CPUs do not multiply across lanes.

## Phase 0 — Fit check (before any init)

**Invoking this skill means "do complex orchestration well." The phased
process below is an exemplar for one topology of task — independent,
file-disjoint tickets — not a mandate to run that shape.** An invocation,
even a direct one from the operator, directs you at the standard of work
(evidence over claims, deliberate dispatch, verified merges); the operator
may not have considered this task's specific shape. Choose the orchestration
shape yourself against the actual topology; when the exemplar fails to fit,
say so and propose the narrower shape before initialising a run.

The exemplar topology fits when tickets are independent, file-disjoint, each
large enough to justify a worktree, and each verifiable by its own test
command.

Signals it does not fit — raise them, don't run through them:

- lanes would edit the **same file** (a shared allowlist, generated manifest,
  one config) — concurrent branches manufacture merge conflicts;
- many tickets are **tiny** — a worktree + branch + dispatch per ten-line
  change is overhead exceeding work; consolidate into batched lanes first;
- tickets are **sequentially coupled** (one consumes another's interface) —
  that is a pipeline, not a fanout;
- the work is one **uniform migration** where review depth matters more than
  wall-clock — fewer, larger lanes beat many shallow ones.

The remedy is a different shape, not a lower standard: consolidate tickets
into fewer lanes, cap concurrency, run lanes serially, or drop to a single
implementer under direct review — carrying the evidence discipline (verify,
ladder, report) into whatever shape you choose, at whatever width it
supports. When the fit is ambiguous and the cost of guessing wrong is high,
ask the operator — one clarifying question beats a conflicted merge train.

## Phase 1 — Init

Write a ticket list and initialise the run. Every lane gets a deterministic
lane-id, branch `lane/<ticket>` and worktree `.claude/worktrees/lane-<ticket>`.

```bash
S=.claude/lanes/<run-id>.json
python .agents/skills/lane-manager/lane_manager.py init --state $S --run-id <run-id> \
  --base-ref <branch you will merge into> --tickets tickets.json
# tickets.json: [{"ticket":"elspeth-…","title":"…","files":["src/…"],"test_command":"pytest tests/… -n 4 -q"}]
```

`files` is the *expected artifact* — read the ticket and name the files a
correct fix must touch. `test_command` is what you will run to verify; it is
not the lane's to choose.

## Phase 2 — Dispatch

For each lane: create the worktree on the branch, **spawn** the worker with a
name equal to the lane-id, and record it. The brief must name the branch,
the worktree CWD, the files, the test command, and the delivery channel
("commit to `lane/<ticket>`; do not touch the main checkout").

```bash
python .agents/skills/lane-manager/lane_manager.py dispatch --state $S --lane <lane-id> --agent-name <lane-id>
```

Add `--dry-run` to record the plan without spawning (used for rehearsals; the
report marks the run DRY RUN). Rungs spent while a lane has only dry-run
attempts are rehearsal: the first real `dispatch` resets that lane's ladder.

## Phase 3 — Verify (on every "done", and before any merge)

```bash
python .agents/skills/lane-manager/lane_manager.py verify --state $S --lane <lane-id>   # exit 0 = landed
```

This runs `git rev-parse` / `git log base..branch` / `git diff --name-only
base...branch`, checks every expected file is in the diff, then runs the test
command in the lane's worktree with `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src`.
Exit 1 means NOT landed; the JSON `reasons` say why. A lane that says "done"
and verifies to exit 1 has lied or misunderstood — escalate (Phase 5). Do not
re-read its message looking for a reason to believe it.

## Phase 4 — A lane goes quiet

A quiet or "idle" lane is **not** a dead lane. Two checks, in order:

1. **list-live** — is the lane's name still among the running workers?
2. Worktree — `idle` inspects uncommitted changes and recent file activity.

```bash
python .agents/skills/lane-manager/lane_manager.py idle --state $S --lane <lane-id> --listed      # or --not-listed
```

Pass `--listed` / `--not-listed` from the **list-live** result you just
obtained. Verdicts: `alive-listed` → **message** it a status ping and wait;
`alive-worktree-activity` → the harness lost track of it but the tree is
moving; verify what exists, then ping; `dead` (exit 1) → both checks failed,
go to Phase 5. Never pass `--not-listed` without having run **list-live**
first, in this same step.

## Phase 5 — Escalation ladder

```bash
python .agents/skills/lane-manager/lane_manager.py escalate --state $S --lane <lane-id> --reason "<evidence>"
```

| Rung | Printed action | You do |
|------|----------------|--------|
| 1 | `nudge` | **message** the lane: name the missing evidence (branch, files, test), ask for the fix on the same branch. Wait for its next message, then verify again. |
| 2 | `redispatch` | **spawn** a fresh worker on the same branch/worktree with the original brief plus the verification `reasons`; `dispatch` it; verify again. |
| 3 | `block` | Lane is BLOCKED with evidence recorded. **Continue with the remaining lanes.** Do not stall the run on one lane. |

Each rung fires once. The ladder cannot be reset by a persuasive message.

## Phase 6 — Report

```bash
python .agents/skills/lane-manager/lane_manager.py report --state $S --out .claude/lanes/<run-id>.report.md
```

One markdown report: landed lanes (branch, commits, files, test exit), blocked
lanes with the ladder and every verification/idle check, and a merge-order
proposal (disjoint file sets first). Merge in that order with `--no-ff`, running
the full suite after each merge — the per-lane test command is a lane gate,
not a tree gate.

## Rationalizations that lose runs

| Thought | Reality |
|---------|---------|
| "The lane pasted its test output; that counts." | Output in a message is text. `verify` runs the command. |
| "It said it committed; git is slow, skip the diff." | `git diff base...branch` takes a second. A lane that lied costs a merge. |
| "Quiet means it crashed — re-dispatch now." | **list-live** + worktree first. Re-dispatching a live lane creates two writers on one branch. |
| "One more nudge and it'll finish." | Two rungs, then BLOCKED. The remaining lanes are waiting on you. |
| "I'll fix the lane's branch myself to get it over the line." | That is a new lane with you as the agent; record it as a re-dispatch or block it. |

## Red flags — stop and run `verify`

- Marking a lane landed from a message.
- A merge without a `verify` exit 0 in the state file's `verifications`.
- `idle --not-listed` with no **list-live** call in the same step.
- A third nudge, a second re-dispatch, or a run that halts on one lane.
