# lane-manager — Claude Code enhancement

Read `SKILL.md` first; this file only binds its three primitives to Claude
Code tools and adds what the harness gives you for free. Nothing here changes
the procedure or the CLI.

## Primitive → tool

| SKILL.md primitive | Claude Code | Notes |
|--------------------|-------------|-------|
| **spawn**(brief, name) | `Agent` with `name: <lane-id>` | Use `subagent_type: general-purpose` (or a project agent type); state the worktree CWD in the first line of the brief — subagents inherit no CWD. |
| **list-live**() | `ListAgents` | Call it in the same turn as `idle --listed/--not-listed`; a listing from an earlier turn is stale. |
| **message**(name, text) | `SendMessage` to the lane's name | Lanes report back to `team-lead`, not to `main`. |

## Idle notifications

Claude Code delivers an `idle_notification` from a subagent as a
`<teammate-message>`. Treat it as the Phase 4 trigger, **not** as a result:

1. `ListAgents` — an agent that is idle-but-listed is alive and waiting; one
   that has vanished is not (memory: idle after a final status usually means
   the agent is gone).
2. `idle --listed` / `--not-listed` accordingly.
3. Act on the verdict; if `alive-listed`, `SendMessage` a status ping and end
   your turn — never sleep-poll a subagent.

A `result` field inside an idle notification is a claim like any other
message: run `verify` before believing it.

## Standing grants on this repo (AGENTS.md)

- Spawn without asking, at any depth and fan-out; idle lanes are the failure
  mode. Brief each lane with an explicit `pytest -n` ceiling.
- The `Workflow` tool is pre-authorised. A lane-manager run is deliberately
  *not* a Workflow — the hub must hold the state file and make the
  ladder decisions — but a lane may use one internally, and a post-run
  adversarial verify of every landed branch is a natural single-phase workflow.
- Lanes never commit to the shared checkout; the hub is the sole writer there.

## Recovering a run in a new session

The state file is the run. `cat .claude/lanes/<run-id>.json`, then for each
non-terminal lane: `ListAgents` (the previous session's agents are gone, so
expect `--not-listed`), `idle`, and continue the ladder from wherever it stopped.
