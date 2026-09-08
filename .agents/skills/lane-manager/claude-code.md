# lane-manager — Claude Code enhancement

Read `SKILL.md` first; this file only binds its three primitives to Claude
Code tools and adds what the harness gives you for free. Nothing here changes
the procedure or the CLI.

## Primitive → tool

| SKILL.md primitive | Claude Code | Notes |
|--------------------|-------------|-------|
| **spawn**(brief, name) | `Agent` with `name: <lane-id>` | Use `subagent_type: general-purpose` (or a project agent type); pass the output of `brief` verbatim — its first line is the worktree CWD, which subagents do not inherit. |
| **list-live**() | `ListAgents` | Call it in the same turn as `liveness --listed/--not-listed` or `resume --listed …`; a listing from an earlier turn is stale. |
| **message**(name, text) | `SendMessage` to the lane's name | Lanes report back to `team-lead`, not to `main`. |

## The one-line return

The brief tells the worker to write `.lanes/<lane-id>/report.md` and return
one line. Claude Code truncates long subagent results and drops them entirely
when the orchestrator is compacted — the file is the deliverable. When a
result arrives, do not act on the line: run `verify`, then read `report.md`
(or `report --run-id …`, which surfaces every summary line).

## Idle notifications

Claude Code delivers an `idle_notification` from a subagent as a
`<teammate-message>`. Treat it as the Phase 4 trigger, **not** as a result:

1. `ListAgents` — an agent that is idle-but-listed is alive and waiting; one
   that has vanished is not (memory: idle after a final status usually means
   the agent is gone).
2. `liveness --listed` / `--not-listed` accordingly.
3. Act on the verdict; if alive, `SendMessage` a status ping and end your turn
   — never sleep-poll a subagent.

A `result` field inside an idle notification is a claim like any other
message: run `verify` before believing it.

## Resuming after this session died

The previous session's agents are gone, so `ListAgents` returns none of them:

```bash
python .agents/skills/lane-manager/lane_manager.py resume --lanes-dir .lanes --run-id <run-id> --redispatch
```

Spawn one `Agent` per brief it prints, named by lane id, and continue the
phases from `status --run-id <run-id>`. Nothing else in `.lanes/` needs
touching; a resumed worker finds the branch, worktree and partial edits where
the killed one left them.

## Standing grants on this repo (AGENTS.md)

- Spawn without asking, at any depth and fan-out; idle lanes are the failure
  mode. Brief each lane with an explicit `pytest -n` ceiling.
- The `Workflow` tool is pre-authorised. A lane-manager run is deliberately
  *not* a Workflow — the hub must hold `.lanes/` and make the ladder decisions
  — but a lane may use one internally, and a post-run adversarial verify of
  every verified branch is a natural single-phase workflow.
- Lanes never commit to the shared checkout; the hub is the sole writer there,
  and `merge` is the only way a lane's work reaches it.
