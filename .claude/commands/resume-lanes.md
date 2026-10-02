---
description: Recover every orchestrator lane after a usage-limit interruption or crash
allowed-tools: Bash(python .agents/skills/orchestrator/orchestrator.py:*), Bash(git worktree list:*), Read, ListAgents
---

# Resume lanes

Recovery after a rate limit, a crash, or a killed orchestrator session. Run the
steps in order and do not skip the dry run.

The control plane is `.orchestrator/lanes.db`; the CLI is
`python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db`.
Full procedure: `.agents/skills/orchestrator/runbook.md`.

## 1. See what the lanes were doing

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db status --stale-after 900
```

Read every lane's phase, model, step count and heartbeat age. A lane marked
`SUSPECTED DEAD` with **holder process ALIVE** is NOT yours to take: something
is still running against that worktree.

## 2. Ask which models are viable BEFORE spawning anything

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db preflight --model <model>
```

Exit 4 means refused. Do not spawn a subagent on a refused model — that is the
silent-death failure mode. The command names the models that are viable; either
re-register the lane onto one of those, or wait for the reset it prints.

If a lane died because its model hit a limit, record that first so nothing
respawns into the same wall:

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db \
  observe-model --model <model> --state exhausted --resets-in <seconds> --source "rate-limit message"
```

## 3. Dry-run the resume

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db resume
```

This writes nothing. For each lane it re-runs every step's verification command
in that lane's worktree and reports the first step that does not pass — the
restart point is **measured, not read from the log**.

Exit codes: `0` clean · `4` a lane is blocked on an exhausted model ·
`5` drift was found. Drift is a finding, not noise:

- `log-wrong` — the log claims a step is done and the tree disagrees. The lane
  was killed mid-write. Expected after a hard kill.
- `unlogged-work` — the tree satisfies a step no instance ever logged. **Stop.**
  That is the signature of a second writer on the worktree. Find out what else
  was running before you resume anything.

## 4. Repair the log and take over the dead lanes

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db \
  resume --execute --take-unknown-locks
```

A usage limit kills every agent at once and agent lanes carry no PID, so their
locks read `unknown`. `--take-unknown-locks` takes those over in one pass. It
never takes a lock whose process is provably alive, and every takeover is
recorded with the fingerprint it displaced.

Then, for each lane you are restarting, spawn ONE worker and have it attach:

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db \
  attach --lane <lane-id> --agent <worker-name>
```

`attach` prints the fencing token. Every later write must carry it
(`heartbeat`, `step-done`, `set-phase`). Exit 3 means the lane is already held —
that is the tool refusing to create a second writer. Do not work around it; find
the holder.

## 5. If a lock will not break

A lock whose holder cannot be proven dead is kept on purpose. Break it only when
you know the instance is gone, naming the fingerprint you are evicting:

```bash
python .agents/skills/orchestrator/orchestrator.py --db .orchestrator/lanes.db \
  evict --lane <lane-id> --fingerprint <holder> --force --reason "<why you know it is gone>"
```

## Report back

State per lane: phase at death, restart step, any drift, and the model verdict.
Never report a lane as resumed before its `attach` has succeeded.
