---
name: lane-manager
description: >
  Use when dispatching several subagent lanes from a ticket list and you must
  know — from git and test evidence on disk, not from what a lane says — which
  lanes landed, which are blocked, and what order to merge them in. Triggers:
  "fan out N tickets", "run these as lanes", a lane reports "done", an idle
  notification arrives for a lane, a lane has gone quiet, or a previous
  orchestrator session died and you must resume its run from .lanes/. Harness-
  neutral; Claude Code users also read claude-code.md in this directory.
---

# lane-manager — durable lanes, evidence-gated verification, serial merge

**A lane's completion message is a claim, not a result.** The only things that
count are on disk and in git: the lane's `status.json`, its `report.md`, the
commits on its branch, and the exit codes `verify` measured in throwaway
worktrees. All of it survives the orchestrator being killed.

This file is the **harness-neutral procedure**. It assumes only that your
harness gives you three primitives, whatever they are called:

| Primitive | Meaning |
|-----------|---------|
| **spawn**(brief, name) | start a worker agent on a brief, addressable by a name you choose |
| **list-live**() | enumerate the worker agents your harness still considers running |
| **message**(name, text) | send text to a running worker |

Everything else is the CLI `python .agents/skills/lane-manager/lane_manager.py`
(`--help` for flags), plain Python + git, and it is the contract. Its state is
documented JSON and markdown under `.lanes/`; any agent, script or human can
read it, and a fresh session can resume from it.

**Running in Claude Code?** Read `claude-code.md` next to this file — it maps
the three primitives to concrete tools and adds harness-specific handling.

## The durable layout

```
.lanes/<run-id>.run.json         base ref, suite command, lane ids, merges (in order)
.lanes/<lane-id>/plan.md         what the lane must do (rendered from the ticket)
.lanes/<lane-id>/status.json     state + heartbeat + attempts + every verification/liveness check
.lanes/<lane-id>/report.md       WRITTEN BY THE WORKER: one-line summary, then findings
```

`state` is one of `pending` → `running` → `verified` → `merged`, with `failed`
reachable from `running` (a verification that disproved the lane, or the
ladder's block rung). Every write is an atomic replace: a kill at any instant
leaves the old file or the new one, never a torn one. `.lanes/` is working
state — keep it out of git.

Each lane has a dedicated git worktree on branch `lane/<ticket>` (created by
`dispatch`, reused by every re-dispatch). Worktree discipline from AGENTS.md
applies (`.venv`, `PYTHONPATH`); the CLI symlinks `.venv` and sets
`PYTHONPATH` itself for the commands it runs. Brief each lane with an explicit
test parallelism ceiling — CPUs do not multiply across lanes.

## Phase 0 — Fit check (before any init)

Invoking this skill means "do complex orchestration well". The phased process
below is an exemplar for independent, file-disjoint tickets, each large enough
to justify a worktree and each verifiable by its own failing-then-passing test.
Signals it does not fit — raise them, don't run through them: lanes would edit
the **same file**; many tickets are **tiny**; tickets are **sequentially
coupled**; the work is one **uniform migration**. The remedy is a different
shape (consolidate, cap concurrency, run serially, single implementer under
review) carrying the same evidence discipline — never a lower standard.

## Phase 1 — Init

```bash
L=.lanes; R=<run-id>
python .agents/skills/lane-manager/lane_manager.py init --lanes-dir $L --run-id $R \
  --base-ref <branch you will merge into> --suite-command "pytest tests/" --tickets tickets.json
# tickets.json: [{"ticket":"elspeth-…","title":"…","description":"…",
#   "files":["src/…"],"test_files":["tests/…"],"test_command":"pytest tests/… -n 4 -q"}]
```

`files` is the *expected artifact* — read the ticket and name the files a
correct fix must touch. `test_files` are where the lane's failing test must
land. `test_command` is what `verify` runs RED then GREEN; `--suite-command`
is the full suite it runs on the merged tree. None of these are the lane's to
choose.

## Phase 2 — Dispatch

```bash
python .agents/skills/lane-manager/lane_manager.py dispatch --lanes-dir $L --lane <lane-id> --agent-name <lane-id>
python .agents/skills/lane-manager/lane_manager.py brief    --lanes-dir $L --lane <lane-id>   # pass VERBATIM to spawn
```

`dispatch` creates the worktree, stamps the heartbeat and sets `running`.
**spawn** the worker with the brief exactly as printed: it names the worktree
CWD, the branch, the files, the test files, the test command, the heartbeat
command to run before each step, and the delivery channel — **write findings
to `.lanes/<lane-id>/report.md` and return exactly one line.** A chat message
can be truncated; the file cannot. Never read a lane's result from its
message; read `report.md` (the `report` command surfaces each summary line).

Add `--dry-run` to record the plan without a worktree or a spawn. Rungs spent
while a lane has only dry-run attempts are rehearsal: the first real dispatch
resets that lane's ladder.

## Phase 3 — Verify (on every "done", and before any merge)

```bash
python .agents/skills/lane-manager/lane_manager.py verify --lanes-dir $L --lane <lane-id>   # exit 0 = verified
```

`verify` never runs in the lane's working directory. It reads git evidence
(branch exists, commits ahead of base, expected files AND test files in the
diff), then in throwaway worktrees it proves the lane went **red → green**
with the **full suite** passing:

1. **RED** — base commit + only the lane's test files: the test command must
   FAIL AT AN ASSERTION, as recorded by the RUNNER. `verify` launches pytest
   with prove-it's `prove_it_red_plugin` (`-p`), which records every
   call-phase report whose outcome is `failed`; RED means that report carried
   a `pytest.fail` or an `AssertionError` raised in a test file. An xfail is
   reported skipped and never counts; an assert inside production code is a
   crash, not the test's assertion. Exit codes and output text are never consulted for this —
   every runner reports an uncaught exception as exit 1 too, and a test
   controls its own output, so a test that prints a verdict and then crashes
   is a crash. Exit 0 means the test passes without the fix and is not a
   failing-first test; a crash (an import of something the fix adds, a
   collection error) is not RED, so the brief tells workers to assert that
   what the fix adds exists (`importlib.util.find_spec`, `hasattr`) before
   using it. Under any runner other than pytest the failure kind is
   **not measurable** and the lane is not verified, for that stated reason:
   give lanes pytest test commands. The record is proof against mistakes,
   not against a lane that deliberately forges it from inside the test
   process — that is what reading the lane's test source is for. Either
   way the lane is `failed` and nothing further is credited.
2. **GREEN** — base merged with the branch (`--no-commit`): the test command
   must exit 0. A conflict here is a failure with the conflict recorded.
3. **SUITE** — the same merged tree: the suite command must exit 0.

Only all three advance the lane to `verified`; the verification records the
base SHA and branch SHA it measured, and that binding is what the merge gate
checks. Exit 1 means `failed`; the JSON `reasons` say why. A lane that says
"done" and verifies to exit 1 has lied or misunderstood — escalate (Phase 5).
Do not re-read its message looking for a reason to believe it.

## Phase 4 — A lane goes quiet, or a session died: resume

A quiet lane is **not** a dead lane. Liveness has three signals, checked in
order, and a lane is dead only when all three are stale:

1. **list-live** — is the worker's name still among the running agents?
2. **heartbeat** — the worker stamps `status.json` before each step.
3. **worktree activity** — a file in the worktree changed inside the window.

Uncommitted changes alone are not life: a lane killed mid-edit leaves them
behind forever.

```bash
python .agents/skills/lane-manager/lane_manager.py liveness --lanes-dir $L --lane <lane-id> --listed|--not-listed
python .agents/skills/lane-manager/lane_manager.py resume   --lanes-dir $L --run-id $R --listed <names…> --redispatch
```

`resume` is the crash path. It reads every `status.json`, classifies each lane
(`killed` = `running` with no live signal; `alive`; `pending`; `terminal`) and
with `--redispatch` records a resumed attempt for the killed lanes only and
prints their briefs — **spawn** one worker per brief and nothing else. Alive
lanes keep running, pending lanes go through Phase 2 normally, verified/
failed/merged lanes are left alone. Pass `--listed` with the names your
**list-live** call returned in this same step; after an orchestrator crash
that is usually nothing, which is fine — the heartbeat and worktree signals
still decide. A resumed worker finds its branch, worktree and partial edits
exactly where the killed one left them.

## Phase 5 — Escalation ladder

```bash
python .agents/skills/lane-manager/lane_manager.py escalate --lanes-dir $L --lane <lane-id> --reason "<evidence>"
```

| Rung | Printed action | You do |
|------|----------------|--------|
| 1 | `nudge` | **message** the lane: name the missing evidence (branch, test files, red/green/suite exit codes), ask for the fix on the same branch. Wait for its next message, then `verify` again. |
| 2 | `redispatch` | `dispatch` again and **spawn** a fresh worker on the same branch/worktree with the brief plus the verification `reasons`; `verify` again. |
| 3 | `block` | Lane is `failed` with the ladder and every check recorded. **Continue with the remaining lanes.** |

Each rung fires once. The ladder cannot be reset by a persuasive message.

## Phase 6 — Merge serially, gated

```bash
python .agents/skills/lane-manager/lane_manager.py report --lanes-dir $L --run-id $R --out .lanes/$R.report.md
python .agents/skills/lane-manager/lane_manager.py merge  --lanes-dir $L --lane <lane-id>   # exit 0 = merged
```

`merge` runs `git merge --no-ff` into the checkout that has the base ref
checked out, but only through the gate: the lane is `verified`, and the base
and branch SHAs are the ones its verification measured. After a merge the base
has moved, so the next lane's verification is stale by construction: `verify`
it again (against the tree that now contains the previous lane), then `merge`.
That loop — verify, merge, verify, merge — is the serial gate; the suite ran
on exactly the tree each merge produces, so there is nothing left to run
after it. Take the order from the report (disjoint file sets first).

## Rationalizations that lose runs

| Thought | Reality |
|---------|---------|
| "The lane pasted its test output; that counts." | Output in a message is text. `verify` runs the command in a clean worktree. |
| "Its test passes, that's enough." | A test that also passes on the base proves nothing. RED is half the evidence. |
| "Quiet means it crashed — re-dispatch now." | **list-live**, heartbeat, worktree. Re-dispatching a live lane creates two writers on one branch. |
| "Verified an hour ago, merge now." | If the base moved, the gate refuses. Re-verify; it is one command. |
| "I'll read its message for the findings." | Read `report.md`. The message was allowed one line for a reason. |
| "One more nudge and it'll finish." | Two rungs, then block. The remaining lanes are waiting on you. |
| "I'll fix the lane's branch myself." | That is a new lane with you as the agent; `dispatch` it or `block` it. |

## Red flags — stop and run `verify`

- Marking a lane verified from a message, or reading findings from a message
  instead of `report.md`.
- A merge without a `verified` state and matching SHAs in `status.json`.
- `liveness --not-listed` or `resume` with no **list-live** call in the same step.
- Re-dispatching a lane that `resume` did not classify as killed.
- A third nudge, a second re-dispatch, or a run that halts on one lane.
