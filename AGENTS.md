# ELSPETH — Agent Guide

ELSPETH (Extensible Layered Secure Pipeline Engine for Transformation and
Handling) is a pipeline engine for building, validating, running, and auditing
LLM/data workflows whose outputs must be reviewed, explained, and reproduced.
Two authoring surfaces — version-controlled YAML and the authenticated Web
Composer (an LLM tool loop) — target one runtime model: the same plugin
contracts, graph validation, executor, Landscape audit trail, and run
accounting. Validation and audit are part of the workflow, not after-the-fact
diagnostics.

## Quick reference

```bash
source .venv/bin/activate      # uv-managed venv (Python 3.12+)
pytest tests/                  # full suite; the plain default selection IS the CI-equivalent run
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth   # static-analysis / trust-tier lint gate
elspeth run --settings examples/<name>/settings.yaml --execute
```

## Gotchas

- **STOP — read [docs/agents/recent-code-hints.md](docs/agents/recent-code-hints.md)
  BEFORE writing code. This is not optional.** Whole-tree AST gates pin the
  EXACT set of dynamic-attribute sites, masquerade sites (tests included),
  wire-shape templates, and output bytes; a locally green scoped run proves
  nothing about them, and one careless `getattr` turns the branch red for
  every sibling (this has happened — 7201beeb7 → elspeth-62a5aa4da8). The doc
  is rolling: when you land a new convention or a new whole-tree trap, add it
  there in the same commit.
- Scoped test runs miss cross-cutting gates — run the full `pytest tests/`
  before merging.
- `elspeth-lints check` requires an explicit `--rules` selection and exits 2
  without one. Until 2026-08-07 it defaulted to `nothing`, so the bare command
  ran zero rules and exited 0 — a green that certified any tree. Whole-repo
  rules also assume `--root` is a real source tree, so scope it
  (`--root src/elspeth`) rather than letting it default to the cwd and walk
  `.venv`/`.uv-cache`. The `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE`
  prefix is what lets a keyless agent run it at all: signature verification
  otherwise demands `ELSPETH_JUDGE_METADATA_HMAC_KEY`, which agents must never
  hold ([O1]). Shape-only verification cannot detect forged judge metadata, so
  a trusted context must re-verify before any merge is authoritative — the
  same treatment CI gives fork PRs.
- That gate currently exits 1 with a large finding corpus. This is the
  deliberate fail-closed state described under "Judge-signature stage", tracked
  as `elspeth-13f0cc04fb` — not a regression you introduced. Compare against
  the corpus before and after your change rather than expecting zero.
- Treat the trust-tier gate as a catch-obvious-bug-hiding check, not a death
  pact. Review every touched file in full, not only changed lines; apply the
  trust-tier rules to production code and clean related tests, configuration,
  and docs to house style. Use approved boundary metadata only for honest
  Tier-3 parsing. If one narrow finding is genuinely policy-wrong, keep the
  clearest correct code and leave it ready for adjudication. Never add aliases,
  padding, reordering, dead code, or semantic distortion merely to preserve or
  reduce signature churn: binding churn is an honest release obligation. Never
  hand-edit signatures; agents
  leave or stage key-free work and the operator signs only when the package or
  release is complete. Keep `AGENTS.md` and `CLAUDE.md` tracked so every
  worktree inherits this posture.
- Validate by trust domain ([ADR-032](docs/architecture/adr/032-validate-by-trust-domain.md)):
  nominally type what ELSPETH owns (`isinstance` against a concrete class we
  define), parse what it does not (sentinel `getattr` + value assertions +
  construct an owned type). Never use a `runtime_checkable` Protocol as a
  security control or a dispatch control — it is structural typing, so an
  impostor passes, widening the protocol silently reclassifies every
  implementation tree-wide (elspeth-8783933d99), and since
  Python 3.12 it silently rejects dynamic-attribute objects such as pydantic
  `extra="allow"` models.
- Worktrees live under `.claude/worktrees/<name>` and symlink `.venv` to the
  main checkout: a bare `uv pip install` inside a worktree clobbers the main
  venv.
- The same trap runs the other way for READS: a bare `python`/`pytest` inside a
  worktree silently imports the MAIN checkout, because the editable install
  resolves `elspeth` there. A cross-commit A/B run that way measures the wrong
  tree and returns a *confidently wrong* answer, not an error. Use
  `PYTHONPATH=<worktree>/src <venv>/bin/python -m pytest ...` and verify
  `elspeth.__file__` points into the worktree before trusting a single result.
- **`<worktree>/src` ALONE IS NOT ENOUGH — it silently leaves `elspeth_lints`
  resolving to the MAIN checkout.** `elspeth_lints` lives in a *separate source
  root* (`elspeth-lints/src/`), so the incantation above puts the worktree's
  `elspeth` on the path and nothing else. Every worktree run of the lints suites
  written that way measured the main checkout's `elspeth_lints` against the
  worktree's `elspeth` — a split-tree result, the same class of confidently-wrong
  answer as an export with no `.git`. Measured 2026-08-26. Use BOTH roots:

  ```bash
  PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src \
    <venv>/bin/python -m pytest ...
  ```

  Verify `elspeth_lints.__file__` as well as `elspeth.__file__`, and note the
  `elspeth-lints` console script is a shebang wrapper hardcoding the main venv's
  interpreter with no `PYTHONPATH` of its own — it honours the variable when the
  parent exports it, and resolves to the main checkout when it does not.
  `elspeth-lints/src` is tracked, so the worktree carries its own copy; it was
  simply never on the path.
- Do not silently switch the shared checkout onto a task branch. If work is
  intended to happen on a branch without creating a separate worktree, surface
  that choice to the user before switching; prefer a dedicated worktree for
  branch-scoped work so the active checkout remains on its current branch.
- The pre-commit secret scanner rescans every line of a touched file, so old
  lines can fire on unrelated edits. Append `# secret-scan: allow-this-line`
  to a false positive; do not bypass the hook with `--no-verify`.
- `git stash` is blocked by a hook — use worktrees or commits instead.
- `AGENTS.md` and `CLAUDE.md` are tracked in git (since 2026-07-28) so fresh
  worktrees inherit them. Commit edits to them like any other file; installer
  upgrades that rewrite these files show up as diffs — review before staging.
- Wardline is NOT part of this project (rolled back 2026-08-29,
  [ADR-043](docs/architecture/adr/043-retire-wardline.md)): do not run
  Wardline scans, do not add `weft.toml`, a `wardline-gate` skill, or an
  `.mcp.json` server for it. It arrived via a Loomweave upgrade, and
  `wardline install` rewrites all of those on every run — a guidance test pins
  their absence. Older plans, ledgers, and hints that cite it are history.
  Trust-boundary honesty is enforced by `elspeth-lints` (`trust_boundary.tests`
  and the masquerade gate) alone.
- Directory-scoped guides exist where the details live:
  `examples/AGENTS.md` (how to run every example) and
  `src/elspeth/plugins/transforms/AGENTS.md` (row data vs audit provenance).

## Project delivery posture

ELSPETH is pre-release software maintained by a single developer. Keep a
process, gate, or document only when it materially improves at least one of:

- reliability of code or tests;
- integrity of code, tests, data, audit evidence, or documentation; or
- supportability of code, deployments, operations, or user workflows.

Plans, run sheets, test procedures, runbooks, and incident diagnostics are
useful process documents and stay when they help build or operate the system.
Update or delete them normally as the system changes.

Do not create signed or sealed plan packages, plan hash manifests, review
receipt sidecars, approval chains, role handoffs, or equivalent organisational
ceremony for documents that will be updated or deleted. This does not prohibit
signatures, checksums, audit chains, or admission gates that protect actual
code, releases, exports, runtime data, or deployed artifacts. If removing a
practice is a marginal call or may discard a real safeguard, surface the tradeoff
to the developer before removing it.

## Composer invariants (non-negotiable)

Two rules govern every change to the Web Composer. Neither is subject to a
latency, cost, or convenience argument. If you believe you need an exception,
STOP and ask the developer before writing code.

**1. The LLM does the job. No composer path bypasses the provider.**
ELSPETH must never synthesize, template, route, match, or otherwise derive
pipeline structure server-side in place of the planner. If the planner is slow,
wrong, or wasteful, that is a planner defect to diagnose — not a reason to
remove the planner from the path. A server-authored graph that reaches the user
as a proposal is banned regardless of what it is called (sketch, recipe, router,
fallback, fast path, synthesis) and regardless of whether it is later
superseded. `provider="server"` must not author pipeline structure.

**2. There are no tutorial-special paths. None. Ever.**
The tutorial runs the same backend as every other session
([ADR-031](docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md)). No
tutorial-only normalization, short-circuit, prompt, or code branch. A defect
visible in the tutorial is a defect in the composer.

Both rules are absolute in the composer's authoring path. They do not prohibit
server-side *validation*, *rejection*, or *redaction* of what the planner
produces, nor the required-control admission gates that protect runtime data.

The interim guided collector guard is LIFTED (WS6, ruling 7878 on
elspeth-88bb77953c): the guided lane authors and projects collectors like any
other node kind, `guided_collector_not_authorable` is retired, and every
`node_type` dispatch site in the guided path and frontend carries a collector
arm or a deliberate documented exclusion. A new node kind or behavior arm is
a parity sweep across those same surfaces (binder, proposal projection +
`validate_payload`, wire cardinality, frontend union/decoder/renderers,
teaching skills) — never a lane-scoped schema narrowing, which stays
unauthorized unless refusal telemetry shows a real tax.

## Standing authorization: skills, subagents, and workflows

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

Optimization priorities when choosing how to work, in order:

1. **Code quality** — correctness, integrity, and maintainability come first.
2. **Wall-clock time** — parallelize (subagents, workflows) to finish sooner.
3. **Efficiency** — token/compute cost matters, but only after the first two.

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

<!-- warpline:instructions:v1.2.0 -->
## Warpline (temporal change-impact)

`warpline` is the Weft federation's temporal / change-impact authority — "if I
touch X, what breaks, and what must I re-verify?". Prefer the MCP tools
(`mcp__warpline__*`); fall back to the `warpline` CLI. Endorsed names and short
shims return identical schema+data.

- `warpline_change_list` / `changed` — changed entities for a rev range; call first.
- `warpline_impact_radius_get` / `blast_radius` — downstream affected set.
- `warpline_reverify_worklist_get` / `reverify` — worklist to recheck before done.
- `warpline_entity_timeline_get` / `timeline`, `warpline_entity_churn_count_get` /
  `churn`, `warpline_edge_snapshot_capture` / `capture_snapshot` (only mutating
  tool; writes `.weft/warpline/` only).

Enrich-only and local-only: every response is `meta.local_only: true`,
`peer_side_effects: []`. `enrichment` is a CLOSED vocab
(`present|absent|unavailable`); sibling absence is explicit, never an implied
clean/allowed state. warpline facts are advisory and never gate. See the
`warpline-workflow` skill for the full loop.
<!-- /warpline:instructions -->

## Judge-signature stage (tier-model allowlist signing)

The trust-tier CI failure is a deliberate fail-closed state: it prevents
unauthorised merges while keeping the outstanding package-level signing work
visible. **Do not attempt to resolve, re-sign, restage, or otherwise clear the
trust-tier CI failure globally during ordinary feature work.** Fix tier-model
defects as you find them, and never make the tier-model state worse. There is
no global obligation for this gate to pass during feature delivery; the global
obligation is to follow the trust-tier standards and avoid introducing new
defects or drift. The operator signs once, at package completion, after churn
has settled.

The `trust_tier.tier_model` lint allowlist seals each judge-gated suppression with an operator-held HMAC signature. Acquiring, repairing, or rotating those signatures runs across a two-actor seam: an agent **stages** a worklist key-free via the `elspeth-judge` MCP server (`mcp__elspeth-judge__*`: `stage_scan` / `stage_status` / `stage_annotate` / `verify_signatures` / `stage_preview` / `stage_rekey`), and the **operator** fires it with the key via the `elspeth-lints` CLI (`sign-bundle` / `rekey`). **Staging asserts; firing verifies** — the operator step re-derives every binding from the live tree and aborts before any write on staleness. An agent must NEVER hold `ELSPETH_JUDGE_METADATA_HMAC_KEY` (the [O1] custody rule, elspeth-fa00de6ec1) and signing never runs in CI. Do not hand-edit a `judge_metadata_signature` or resurrect the old per-release signing runbooks — stage a bundle and have the operator fire it. All judging — including the final signature verdict — normally runs on the Codex CLI harness with read-only tool access (`--judge-transport codex-cli --judge-tools readonly`): the judge explores the tree before ruling, and its rationale is secret-scrubbed before persist. The legacy `agent` transport is accepted with read-only tools but is not the normal signing path. The full workflow lives in the `judge-signature-workflow` skill and `docs/judge-signature-handoff.md`.
