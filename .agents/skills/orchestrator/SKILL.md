---
name: orchestrator
description: >
  Use when running several agent lanes in parallel across git worktrees and you
  must not lose them to a quota limit, a kill, or a duplicate instance. Triggers:
  "fan out N lanes", spawning a subagent on a model that may be rate-limited, a
  lane has gone quiet, a usage limit killed the session, a reply appears to have
  forked an agent, or shared lane state looks like it has two writers. Owns the
  single-writer lock, the model preflight and the resumable step log; pair it
  with lane-manager, which owns verification and merge.
---

# orchestrator — single-writer lanes, model preflight, evidence-based resume

**A lane you cannot prove is dead is a lane you must not take.** That one rule
is the difference between recovering a run and creating a phantom second writer
that corrupts the worktree and burns the rest of your quota on self-inflicted
debugging.

State lives in `.orchestrator/lanes.db` (SQLite, gitignored). The CLI is
`python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db`
(`--help` for flags). The operator procedure is [runbook.md](runbook.md); the recovery slash
command is `/resume-lanes`.

## Division of authority with lane-manager

Do not duplicate lane state. These own different facts and join on `lane_id`:

| Fact | Authority |
|------|-----------|
| who may write to a lane; its model, phase, step log | **this skill** (`.orchestrator/lanes.db`) |
| red→green→suite evidence; merge order and gating | `lane-manager` (`.lanes/`) |

This skill never decides whether a lane's work is *correct*. Heartbeats are not
evidence. Run `lane-manager verify` for that.

## The loop

```bash
O="python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db"

$O register --lane <id> --worktree <path> --branch <branch> \
            --model <model> --task "<what it must do>" --steps-json steps.json
$O preflight --model <model>            # exit 4 = DO NOT SPAWN
FP=$($O attach --lane <id> --agent <name> | jq -r .fingerprint)
$O step-done --lane <id> --fingerprint "$FP" --seq N     # stamps the heartbeat too
$O status --stale-after 900
$O resume --execute --take-unknown-locks                 # after a rate limit
```

`steps.json` is `[{"name": …, "verify_cmd": …}]`. `verify_cmd` must **measure
the artifact** (`test -f`, `find_spec`), be cheap, and be safe to re-run: it is
what `resume` uses to decide the restart point, and a command that always
succeeds makes the lane permanently un-resumable.

## Non-negotiables

1. **Never spawn without `preflight`.** A subagent on an exhausted model dies
   silently; you find out when the report never arrives. Exit 4 means stop, and
   the output names the models that are viable.
2. **Every write carries the fencing token.** `attach` mints it from `secrets`;
   `heartbeat`, `step-done` and `set-phase` all take `--fingerprint`. An
   instance that no longer holds the lane is refused with exit 3.
3. **Exit 3 is a finding, not an obstacle.** Two fingerprints differing only in
   the random suffix is a forked duplicate. Stop the fork; do not evict the
   holder to make your agent run.
4. **`SUSPECTED DEAD` is about the heartbeat, not the holder.** Read the holder
   clause. A quiet lane whose process is alive is slow, not dead.
5. **`resume` re-verifies; it does not trust the log.** `unlogged-work` drift
   means the tree satisfies a step nobody logged — a second writer. Investigate
   before resuming.
6. **Record what you observe of a model.** `observe-model` is what makes
   preflight useful; the table has no oracle behind it.

## Rationalizations that lose runs

| Thought | Reality |
|---------|---------|
| "The lock is stale, I'll just take it." | Only `dead-process` and `dead-boot` are proof. `unknown` is your call to make explicitly, and it is recorded. |
| "It's been quiet an hour, it's dead." | Check the holder clause. Killing a live lane is how you get two writers. |
| "The step log says step 3 is done." | The log is a claim. `resume` re-runs the predicate; the tree decides. |
| "I'll spawn on opus and see what happens." | That is failure mode 1. `preflight` costs one command. |
| "Two agents on one lane is fine, they'll coordinate." | They will not. That is what the fencing token exists to prevent. |
| "I'll drop the `--fingerprint` to keep the command short." | Then the write is unfenced, and the mechanism is decorative. |
