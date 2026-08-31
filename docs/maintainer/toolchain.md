# Maintainer toolchain

**This document describes how the maintainer works with their own agents. It
is not a requirement of the project.** Contributors are free to use any
editor, harness, tracker, or no agent tooling at all; nothing below is needed
to build, test, or contribute to ELSPETH, and no CI gate or review checks for
it. The project-level covenant — the invariants every contributor and agent
must honour — lives in [`AGENTS.md`](../../AGENTS.md).

Until 2026-08-30 this material sat in `AGENTS.md` itself, which meant the
maintainer's tool choices read as project mandates. It was split out under
[ADR-043](../architecture/adr/043-project-tooling.md) so that the public
covenant stays short and harness-neutral. The Filigree and Loomweave blocks
below are installer-written mirrors: their installers rewrite the block
between the `<!-- <tool>:instructions -->` markers on every run. Their exact
placement is installer-owned and may also include `AGENTS.md` or `CLAUDE.md`;
this document must not claim exclusive custody of those blocks.

The tools named here are the ones the maintainer's agents currently carry
standing instructions for. The first-party `elspeth-lints` package and its
judge-signature seam are *product*, not toolchain — they are documented in
`AGENTS.md`, [`docs/judge-signature-handoff.md`](../judge-signature-handoff.md),
and `docs/elspeth-lints/`.

## Standing authorization: skills, subagents, and workflows

This is the maintainer's grant to their own agents. It is reproduced verbatim
because the agents act on the wording.

**Use skills, subagents, nested subagents, and multi-agent workflows liberally
and at your own discretion.** This is a standing user request, not a
conditional permission: spawn without asking, at whatever depth and fan-out the
work warrants. A subagent may spawn its own subagents freely, and so may those
— declare the delegation up the chain (what, why, which file lanes) and keep
reporting hub-and-spoke, but do not ask for permission and do not cap depth or
count on caution grounds. Idle lanes are the failure mode; conservative spawn
limits are not a virtue here.

**The Workflow tool is included in that grant.** Its default posture requires
explicit per-use opt-in; on ELSPETH this paragraph IS that opt-in, standing for
every session and every agent, and it satisfies the tool's own condition that
the user asked for multi-agent orchestration. Author and run a workflow whenever
the work has the shape workflows are for — fan-out over a discovered work-list,
adversarial verify of each finding, a judge panel over independent approaches,
a migration sweep too large for one context — without stopping to ask or to
price it out first. Prefer several single-phase workflows in sequence, reading
each result before choosing the next phase, over one monolith. Token cost is
the third priority, behind correctness and wall-clock (below); it is not a
reason to decline the shape.

The one thing this grant does not reach is the session's **"Dynamic workflow
size"** setting, which the harness applies to workflow agent counts and which
only the operator can change in `/config`. Work within whatever it says; if a
task genuinely needs a wider tree than it allows, say so rather than
silently under-scoping the workflow.

**This is high-assurance work, which does not merely permit wide dispatch — it
close to requires it.** ELSPETH's thesis is that validation and audit belong
inside the workflow rather than after it, and independent agent lanes are that
same posture applied to building the system: adversarial review, red/green
cross-checks, and multi-lens verification only carry evidential weight when the
lenses are genuinely independent contexts, not one agent re-reading its own
reasoning. Under-dispatching is the real risk on this project. A wide tree is
the cheap half of a change whose expensive half is a wrong fix reaching an
audit trail.

No constraint on dispatch volume or depth exists anywhere in this guide, and
none may be re-accumulated. That lift is about orchestration shape only — the
rest of this guide's substantive and safety guidance is untouched, including
the [O1] judge-key custody rule, shared-checkout write discipline (stage only your
own pathspecs; never `git restore`/`clean` files you did not stage), venv and
worktree CWD discipline, operator gates on destructive shared-state actions,
and the box's finite resources — a wide fan-out must brief an explicit
per-agent test-parallelism ceiling, because 24 CPUs do not multiply.

When fanning several lanes out from a ticket list, use the `lane-manager`
skill (`.claude/skills/lane-manager/`): it keeps a per-run state file under
`.claude/lanes/`, verifies every lane from `git log`/`git diff` and the lane's
test command rather than its own report, checks the live-agent list + the
worktree before calling an idle lane dead, escalates nudge → re-dispatch →
BLOCKED without stalling, and emits the landed/blocked/merge-order report.
`SKILL.md` is harness-neutral (Codex too); `claude-code.md` beside it is the
Claude Code binding.

Optimization priorities when choosing how to work, in order:

1. **Code quality** — correctness, integrity, and maintainability come first.
2. **Wall-clock time** — parallelize (subagents, workflows) to finish sooner.
3. **Efficiency** — token/compute cost matters, but only after the first two.

## Retired tools

Wardline is NOT part of this project (rolled back 2026-08-29,
[ADR-043](../architecture/adr/043-project-tooling.md)): do not run Wardline
scans, do not add `weft.toml`, a `wardline-gate` skill, or an `.mcp.json`
server for it. It arrived via a Loomweave upgrade, and `wardline install`
rewrites all of those on every run — a guidance test pins their absence. The
same ADR retires Legis and Warpline; the project ships no tool integrations
beyond the ones it names, and contributors use whatever tools they like.
Older plans, ledgers, and hints that cite the retired tools are history.
Trust-boundary honesty is enforced by `elspeth-lints` (`trust_boundary.tests`
and the masquerade gate) alone.

<!-- filigree:instructions:v3.1.0:65e6fb25 -->
<!-- filigree:last-writer:filigree install -->
## Filigree Issue Tracker

`filigree` tracks tasks for this project. Data lives in `.filigree/`. Prefer
the MCP tools (`mcp__filigree__*`) when available; fall back to the `filigree`
CLI otherwise.

### Workflow

```bash
# At session start
filigree session-context                            # ready / in-progress / critical path

# Pick up the next startable issue (atomic claim + transition into its working status)
filigree start-next-work --assignee <name>
# ...or claim a specific issue
filigree start-work <id> --assignee <name>

# Do the work, commit, then
filigree close <id>
```

Use the atomic claim+transition verbs — `work_start` / `work_start_next`
(MCP) or `start-work` / `start-next-work` (CLI). Do **not** chain
`work_claim` (MCP) or `filigree claim` (CLI) with a subsequent status
update — the two-step form races against other agents; the combined verb is
atomic.

**Ready ≠ startable.** The working status is type-specific (tasks →
`in_progress`, features → `building`). Bugs start at `triage`, which has no
single-hop transition into work (`triage → confirmed → fixing`), so a triage
bug is *ready* but not directly *startable*: `work_start` on one returns
`INVALID_TRANSITION` naming the next status, and `work_start_next` skips it.
`work_ready` items carry a `startable` flag (plus a `next_action` hint when
false). Pass `advance=true` (MCP) / `--advance` (CLI) to walk the soft
transitions to the nearest working status automatically.

### Observations: when (and when not) to use them

`observation_create` is a fire-and-forget scratchpad for *incidental* defects — things
you notice *outside the scope of your current task* (a code smell in a
neighbouring file, a stale TODO, a missing test for an edge case you happened
to spot). Notes expire after 14 days unless promoted. Include `file_path` and
`line` when relevant. At session end, skim `observation_list` and either
`observation_dismiss` or `observation_promote` for what has accumulated.

**You fix bugs in your currently defined scope. You do NOT use observations
to finish work prematurely.** If a defect, gap, or follow-up belongs to your
current task, you own it — handle it as part of that task: fix it now, expand
the task's scope, file a proper issue with a dependency, or surface it to the
user. Filing it as an observation and closing the task is *not* completing
the task; it is shipping known-broken work and hiding the debt in a 14-day
expiring scratchpad. The test is "would I have noticed this even if I weren't
working on this task?" If no, it's task scope, not an observation.

### Priority scale

- P0: Critical (drop everything)
- P1: High (do next)
- P2: Medium (default)
- P3: Low
- P4: Backlog

### Reaching for tools

MCP tool schemas describe each tool; `filigree --help` and `filigree <verb>
--help` are the authoritative CLI reference. You do not need to memorise
either catalogue. The verbs you will reach for most:

- **Find work:** `work_ready`, `work_blocked`, `issue_list`, `issue_search`
- **Claim work:** `work_start`, `work_start_next`
- **Update:** `comment_add`, `label_add`, `issue_update`, `issue_close`
- **Admin (irreversible):** `issue_delete` (MCP) / `delete-issue` (CLI) —
  hard-deletes a terminal issue and its rows; `admin_undo_last` cannot reverse it.
- **Scratchpad:** `observation_create`, `observation_list`, `observation_promote`, `observation_dismiss`
- **Cross-product entity bindings (ADR-029):** `entity_association_add`,
  `entity_association_remove`, `entity_association_list`,
  `entity_association_list_by_entity`. Used when a sibling tool (e.g.
  Loomweave) needs to bind a Filigree issue to a function, class, or
  module identifier it owns. The `entity_id` is an opaque external string
  from Filigree's perspective and may be a `loomweave:eid:...` SEI or a legacy
  locator; callers may also supply `entity_kind` explicitly. The consumer (the sibling tool's read
  path) does drift detection against the stored
  `content_hash_at_attach`. `entity_association_list_by_entity` is the
  reverse-lookup surface — given an opaque external entity ID, return every
  Filigree issue bound to it (project isolation is by DB file). Also
  reachable over HTTP as
  `GET/POST /api/issue/{issue_id}/entity-associations`,
  `DELETE /api/issue/{issue_id}/entity-associations?entity_id=…`,
  and `GET /api/entity-associations?entity_id=…`.
- **Health:** `stats_get`, `metrics_get`, `mcp_status_get`

Pass `--actor <name>` (CLI) so events attribute to your agent identity. It
works in either position — before the verb (`filigree --actor X update …`) or
after it (`filigree update … --actor X`); the post-verb value overrides the
group-level one.

### Error handling

Errors return `{error: str, code: ErrorCode, details?: dict}`. Switch on
`code`, not on message text. Codes: `VALIDATION`, `NOT_FOUND`, `CONFLICT`,
`INVALID_TRANSITION`, `PERMISSION`, `NOT_INITIALIZED`, `IO`,
`INVALID_API_URL`, `FILE_REGISTRY_DISPLACED`, `REGISTRY_UNAVAILABLE`,
`LOOMWEAVE_REGISTRY_VERSION_MISMATCH`, `LOOMWEAVE_OUT_OF_SYNC`,
`BRIEFING_BLOCKED`, `STOP_FAILED`, `SCHEMA_MISMATCH`, `INTERNAL`.

On `INVALID_TRANSITION`, call `workflow_transition_list` (MCP) or
`filigree transitions <id>` to see what the workflow allows from here.

Two failure modes deserve a specific response:

- **`SCHEMA_MISMATCH`** — the installed `filigree` is older than the project
  database. The error message contains upgrade guidance. Surface it to the
  user; do not retry.
- **`ForeignDatabaseError`** — filigree found a parent project's database
  but no local `.filigree.conf`. Run `filigree init` in the current
  directory. Do **not** `cd` upward to a different project unless that was
  the actual intent.
<!-- /filigree:instructions -->

<!-- loomweave:instructions:v1.5.0:39edbf6d -->
<!-- loomweave:last-writer:loomweave install -->
## Loomweave (code structure + SEI identity)

Loomweave pre-extracts this repo into a queryable map — entities, their
call/reference/import/relation edges, and subsystems — each carrying a Stable
Entity Identity (SEI). Ask its `mcp__loomweave__*` tools, not grep, for "what
calls X", "what subclasses X", "where is X defined", "find the thing that
does Y".

- Never hand-construct an entity id: take it from `entity_find` / `entity_at` /
  `entity_resolve`, and bind cross-tool records on the `sei`, not the `id`.
- If `project_status_get` reports stale, re-index before answering.

Full reference: `loomweave-workflow` skill, `loomweave --help`, MCP schemas.
<!-- /loomweave:instructions -->

### ELSPETH's Loomweave usage

The block above is installer-written. ELSPETH's own guidance
([ADR-043](../architecture/adr/043-project-tooling.md)):

- **Reach for it for:** who calls X (`entity_callers_list`), what subclasses
  or implements X (`entity_relation_list`, `direction=in`), execution paths /
  call trees (`entity_execution_path_list`, `entity_orientation_pack_get`),
  and where X is defined in a large file (`entity_find`). These measured
  correct against `ast` ground truth and have no grep equivalent.
- **Do not rely on it for** semantic search, dead-code lists, HTTP-route
  inventories, or test-caller lists until the salvage worklist closes; on
  those `git grep` measured as good or better.
- `entity_find` takes **`pattern`** — not `name`, not `query`; 43 % of all
  historical calls failed on that.
- **Zero callers is not "no callers".** Read `traversal_complete`,
  `scope_excludes`, and `unresolved_candidates` before concluding; class
  instantiations (never resolved to a call edge) and calls from files analyzed
  by a venv-less hook run may sit there as `why: dynamic`. Confirm a negative
  with `git grep`.
- Check `project_status_get` staleness first; a re-analyze is triggered by
  the git hooks on the main checkout, not by worktree commits. Any list over
  ~100 rows overflows the MCP result cap — page with the cursor.

## Judge-signature seam: how the maintainer's agents use it

The seam itself is product and is specified in `AGENTS.md` and
[`docs/judge-signature-handoff.md`](../judge-signature-handoff.md). The
maintainer's convention on top of it: judging — including the final signature
verdict — runs on the Codex CLI harness with read-only tool access
(`--judge-transport codex-cli --judge-tools readonly`), so the judge explores
the tree before ruling. The legacy `agent` transport is accepted with
read-only tools but is not the normal signing path. The `judge-signature-workflow`
skill carries the step-by-step procedure.
