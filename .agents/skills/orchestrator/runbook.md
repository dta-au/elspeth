# Orchestrator runbook (operator)

How to run 3–8 parallel agent lanes across git worktrees and not lose work when
a model runs out of quota, a usage limit kills the session, or an agent forks
into two instances. Read `SKILL.md` first; this is the procedure.

State: `.orchestrator/lanes.db` (SQLite, gitignored). Recovery: `/resume-lanes`.

## What this owns, and what it does not

Two lane systems, different facts, joined on `lane_id`:

| Question | Authority |
|----------|-----------|
| Who may write to this lane right now? | **`.orchestrator/lanes.db`** |
| Which model is it on, and is that model viable? | **`.orchestrator/lanes.db`** |
| What phase was it in, and which steps are done? | **`.orchestrator/lanes.db`** |
| Did the lane go red→green with the suite passing? | `.lanes/` (`lane-manager`) |
| Is the branch safe to merge, and in what order? | `.lanes/` (`lane-manager`) |

This never decides whether a lane's work is *correct*. Heartbeats are not
evidence; `lane-manager verify` is.

## Setup

```bash
O="python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db"

cat > /tmp/steps.json <<'JSON'
[{"name": "failing-test", "verify_cmd": "test -f tests/unit/test_thing.py"},
 {"name": "implement",    "verify_cmd": "python -c \"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('elspeth.thing') else 1)\""},
 {"name": "suite",        "verify_cmd": "test -f .lane-suite-green"}]
JSON

$O register --lane elspeth-abc123 \
  --worktree .claude/worktrees/thing --branch lane/thing \
  --model claude-opus-5 --task "fix the join bug" --steps-json /tmp/steps.json
```

**`verify_cmd` is the load-bearing field.** It is what `resume` re-runs to decide
whether a step is really done, so it must measure the artifact, be cheap, and be
safe to re-run. `test -f` on a file the step creates is fine; `echo done` is not
— it makes the step permanently "complete" and resume blind.

Every step must be idempotent: resume restarts at the first step whose
`verify_cmd` fails, and a partially-completed step re-runs from the top.

## Before spawning anything: preflight

```bash
$O preflight --model claude-opus-5        # exit 4 = DO NOT SPAWN
```

The exhaustion table is fed by **observation** — there is no local API reporting
Claude Code subagent quota. When a lane dies of a rate limit, record it so
nothing respawns into the same wall:

```bash
$O observe-model --model claude-opus-5 --state exhausted --resets-in 3600 --source "usage limit message"
$O observe-model --model claude-sonnet-5 --state available --source "spawned ok"
```

A model with no observation reads `unknown`, **not** available. Preflight lets an
unknown model through by default (refusing everything unobserved would block
every first run) and says so. `--require-verified` refuses it instead.

## Running a lane

```bash
FP=$($O attach --lane elspeth-abc123 --agent thing-worker | jq -r .fingerprint)

$O set-phase --lane elspeth-abc123 --fingerprint "$FP" --phase implementing
$O step-done --lane elspeth-abc123 --fingerprint "$FP" --seq 1
$O heartbeat --lane elspeth-abc123 --fingerprint "$FP"
```

`step-done` stamps the heartbeat in the **same transaction** as the step, so "a
heartbeat on every step" is a property of the schema, not a habit.

Pass `--pid <pid>` when the lane really is a process you can point at (a script,
a shell: `--pid $$`). Then a kill is *provable* and recovery is automatic. An
agent lane usually has no such process; omit it and the lock is held with
liveness `unknown` — safe, but needing an explicit takeover.

## Monitoring

```bash
$O status --stale-after 900
```

```
lane-a                   SUSPECTED DEAD (quiet 5410s / 1h30m; holder unknown)
    model=claude-opus-5  phase=implementing  steps=1/3
    worktree=/…/wt-a  branch=lane/a
    task=fix the join bug
```

`SUSPECTED DEAD` is about the **heartbeat**, not permission to take the lane.
Read the holder clause:

| Holder clause | Meaning | Action |
|---------------|---------|--------|
| `holder process ALIVE - DO NOT resume` | its process is running | leave it; slow, not dead |
| `holder dead-process` | its process is gone | resume takes it automatically |
| `holder dead-boot` | the machine rebooted since | resume takes it automatically |
| `holder unknown` | nothing to check against | **your** call; see takeover |

## Recovery after a rate limit

```bash
$O resume                                     # dry run, writes nothing
$O resume --execute --take-unknown-locks      # the usual recovery
```

`resume` walks each lane's step log in order and **re-runs every `verify_cmd`**.
The log is a claim; the tree is the measurement. The restart point is the first
step that does not pass, whatever the log says.

Exit: `0` clean · `1` a lane's worktree no longer exists · `4` blocked on an
exhausted model · `5` drift.

A lane whose worktree is missing is reported as `blocked-worktree-missing`
with **no** restart point. Every `verify_cmd` would fail there, but none of
them failed because the work is undone — re-create the worktree first.

Two kinds of drift, both reported, never silently corrected:

- **`log-wrong`** — the log says done, the tree disagrees. A kill mid-write.
  Expected after a hard kill; `--execute` repairs the log.
- **`unlogged-work`** — the tree satisfies a step **no instance ever logged**.
  Stop. That is a second writer on that worktree. Find out what else was running
  before resuming anything.

### Taking over locks after every agent died

A usage limit kills every agent at once, and agent lanes carry no PID, so every
lock reads `unknown`. `--take-unknown-locks` takes those over in one pass. It
never takes a lock whose process is provably alive, acts only under `--execute`,
and records every takeover with the fingerprint it displaced.

That is the safety property: the tool refuses to *guess* a holder is dead. You
assert it, and the assertion is recorded.

### One stubborn lane

```bash
$O evict --lane elspeth-abc123 --fingerprint <holder> --force --reason "session killed at 14:02"
```

You must name the fingerprint being evicted; the wrong one fails.

## When a duplicate instance appears

```
LOCK HELD: lane 'lane-a' is held and its holder cannot be proven dead.
  holder=worker-a-4046160-42c1c5a77e0d3c0f568887c6878e7652
  refused=worker-a-FORK-4046250-3909ef6d3bf638043fed18df2024778a
  liveness=unknown
Nothing was changed.
```

Exit 3, nothing written. The two fingerprints share the agent name and differ
only in the random suffix — that is the fork being caught. **Do not evict the
holder to make your agent run.** Find out why there are two; if a reply to a
running agent forked it, the fork is the one to stop.

An instance evicted while it was away cannot resume writing: its fingerprint no
longer holds the lane, so its heartbeats and step writes are refused with exit 3.
That is the phantom second writer failing closed.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | usage or state error (no such lane, no such step, a lane whose worktree is gone) |
| 3 | lock held, or a write from an instance that does not hold the lane |
| 4 | model refused by preflight, or a lane blocked on an exhausted model |
| 5 | resume found drift between the step log and the tree |

## What this deliberately does not do

- **Spawn agents.** That is the orchestrating agent's job; this says whether
  spawning is safe and what to spawn on.
- **Judge a lane's work.** `lane-manager verify` does, with red→green→suite
  evidence in throwaway worktrees.
- **Probe model quota by itself.** No local oracle exists, so the table is
  observation-fed and says `unknown` when it does not know.
- **Break a lock it cannot prove is dead.** Default deny. An unverifiable lock
  silently treated as stale is exactly how a phantom second writer is created.
