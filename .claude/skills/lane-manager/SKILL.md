---
name: lane-manager
description: >
  Use when dispatching several subagent lanes from a ticket list and you must
  know — from git and test evidence, not from what a lane says — which lanes
  landed, which are blocked, and what order to merge them in. Triggers: "fan
  out N tickets", "run these as lanes", a lane reports "done", an idle
  notification arrives for a lane, or a lane has gone quiet and you are
  deciding whether to re-dispatch. Also use when recovering a previous run from
  a state file under .claude/lanes/.
user-invocable: true
---

# lane-manager — dispatch lanes, verify from evidence, escalate, report

**A lane's completion message is a claim, not a result.** The only things that
count are: commits on the expected branch, the expected files in that diff, and
the lane's test command exiting 0 — all measured by you. This skill keeps one
JSON state file per run under `.claude/lanes/` and drives every decision from
`python .claude/skills/lane-manager/lane_manager.py` (`--help` for flags).

Companion skills: `superpowers:dispatching-parallel-agents` (brief shape),
`superpowers:using-git-worktrees` (one worktree per lane — AGENTS.md worktree
and `.venv` discipline apply). ELSPETH standing grants: spawn without asking,
cap each lane's `pytest -n` so 24 CPUs are not oversubscribed.

## Phase 1 — Init

Write a ticket list and initialise the run. Every lane gets a deterministic
lane-id, branch `lane/<ticket>` and worktree `.claude/worktrees/lane-<ticket>`.

```bash
S=.claude/lanes/<run-id>.json
python .claude/skills/lane-manager/lane_manager.py init --state $S --run-id <run-id> \
  --base-ref <branch you will merge into> --tickets tickets.json
# tickets.json: [{"ticket":"elspeth-…","title":"…","files":["src/…"],"test_command":"pytest tests/… -n 4 -q"}]
```

`files` is the *expected artifact* — read the ticket and name the files a
correct fix must touch. `test_command` is what you will run to verify; it is
not the lane's to choose.

## Phase 2 — Dispatch

For each lane: create the worktree on the branch, spawn the agent with a
`name` equal to the lane-id, and record it. The brief must name the branch,
the worktree CWD, the files, the test command, and the delivery channel
("commit to `lane/<ticket>`; do not touch the main checkout").

```bash
python .claude/skills/lane-manager/lane_manager.py dispatch --state $S --lane <lane-id> --agent-name <lane-id>
```

Add `--dry-run` to record the plan without spawning (used for rehearsals; the
report marks the run DRY RUN). Rungs spent while a lane has only dry-run
attempts are rehearsal: the first real `dispatch` resets that lane's ladder.

## Phase 3 — Verify (on every "done", and before any merge)

```bash
python .claude/skills/lane-manager/lane_manager.py verify --state $S --lane <lane-id>   # exit 0 = landed
```

This runs `git rev-parse` / `git log base..branch` / `git diff --name-only
base...branch`, checks every expected file is in the diff, then runs the test
command in the lane's worktree with `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src`.
Exit 1 means NOT landed; the JSON `reasons` say why. A lane that says "done"
and verifies to exit 1 has lied or misunderstood — escalate (Phase 5). Do not
re-read its message looking for a reason to believe it.

## Phase 4 — Idle notification

An idle notification is **not** a death certificate. Two checks, in order:

1. `ListAgents` — is the lane's agent name still listed?
2. Worktree — `idle` inspects uncommitted changes and recent file activity.

```bash
python .claude/skills/lane-manager/lane_manager.py idle --state $S --lane <lane-id> --listed      # or --not-listed
```

Verdicts: `alive-listed` → send it a status ping via SendMessage and wait;
`alive-worktree-activity` → the harness lost track of it but the tree is
moving; verify what exists, then ping; `dead` (exit 1) → both checks failed,
go to Phase 5. Never call `idle --not-listed` without having called
`ListAgents` in this turn.

## Phase 5 — Escalation ladder

```bash
python .claude/skills/lane-manager/lane_manager.py escalate --state $S --lane <lane-id> --reason "<evidence>"
```

| Rung | Printed action | You do |
|------|----------------|--------|
| 1 | `nudge` | SendMessage the lane: name the missing evidence (branch, files, test), ask for the fix on the same branch. Wait for its next message, then verify again. |
| 2 | `redispatch` | Spawn a fresh agent on the same branch/worktree with the original brief plus the verification `reasons`; `dispatch` it; verify again. |
| 3 | `block` | Lane is BLOCKED with evidence recorded. **Continue with the remaining lanes.** Do not stall the run on one lane. |

Each rung fires once. The ladder cannot be reset by a persuasive message.

## Phase 6 — Report

```bash
python .claude/skills/lane-manager/lane_manager.py report --state $S --out .claude/lanes/<run-id>.report.md
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
| "Idle means it crashed — re-dispatch now." | `ListAgents` + worktree first. Re-dispatching a live lane creates two writers on one branch. |
| "One more nudge and it'll finish." | Two rungs, then BLOCKED. The remaining lanes are waiting on you. |
| "I'll fix the lane's branch myself to get it over the line." | That is a new lane with you as the agent; record it as a re-dispatch or block it. |

## Red flags — stop and run `verify`

- Marking a lane landed from a message.
- A merge without a `verify` exit 0 in the state file's `verifications`.
- `idle --not-listed` with no `ListAgents` call this turn.
- A third nudge, a second re-dispatch, or a run that halts on one lane.
