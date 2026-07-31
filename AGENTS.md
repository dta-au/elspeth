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
elspeth-lints check            # static-analysis / trust-tier lint gate
elspeth run --settings examples/<name>/settings.yaml --execute
```

## Gotchas

- Scoped test runs miss cross-cutting gates — run the full `pytest tests/`
  before merging.
- Validate by trust domain ([ADR-032](docs/architecture/adr/032-validate-by-trust-domain.md)):
  nominally type what ELSPETH owns (`isinstance` against a concrete class we
  define), parse what it does not (sentinel `getattr` + value assertions +
  construct an owned type). Never use a `runtime_checkable` Protocol as a
  security control — it is structural typing, so an impostor passes, and since
  Python 3.12 it silently rejects dynamic-attribute objects such as pydantic
  `extra="allow"` models.
- Worktrees live under `.claude/worktrees/<name>` and symlink `.venv` to the
  main checkout: a bare `uv pip install` inside a worktree clobbers the main
  venv.
- The pre-commit secret scanner rescans every line of a touched file, so old
  lines can fire on unrelated edits. Append `# secret-scan: allow-this-line`
  to a false positive; do not bypass the hook with `--no-verify`.
- `git stash` is blocked by a hook — use worktrees or commits instead.
- `AGENTS.md` and `CLAUDE.md` are tracked in git (since 2026-07-28) so fresh
  worktrees inherit them. Commit edits to them like any other file; installer
  upgrades that rewrite these files show up as diffs — review before staging.
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

## Standing authorization: skills and subagents

Agents are always authorized to invoke skills and dispatch subagents at their
sole discretion — no per-use permission is needed. Any constraints stated
elsewhere in this guide or in project-specific guidance still apply (e.g. the
[O1] judge-key custody rule, worktree/venv discipline, operator gates on
destructive shared-state actions).

Optimization priorities when choosing how to work, in order:

1. **Code quality** — correctness, integrity, and maintainability come first.
2. **Wall-clock time** — parallelize (subagents, workflows) to finish sooner.
3. **Efficiency** — token/compute cost matters, but only after the first two.

<!-- filigree:instructions:v3.1.0:c1c023c3 -->
<!-- filigree:last-writer:filigree install -->
## Filigree Issue Tracker

`filigree` tracks this project's work. Use it to find, claim, update and close
issues: `filigree session-context` at session start, then
`filigree start-next-work --assignee <name>`.

Full reference: the **filigree-workflow** skill (patterns, priorities,
observations, error codes), `filigree --help`, and the `mcp__filigree__*` tool
schemas. Prefer the MCP tools when available; fall back to the CLI.

Two rules `--help` will not tell you:

1. Claim atomically: `work_start` / `work_start_next` (MCP) or `start-work` /
   `start-next-work` (CLI). Never chain a claim with a separate status update;
   that two-step form races other agents.
2. On `SCHEMA_MISMATCH` the installed filigree is older than the project
   database. Surface it to the user; do not retry.
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

<!-- legis:instructions:v1.5.0:37065fbc -->
## Legis (git/CI + governance)

Legis is the git/CI and governance layer of the Weft suite: graded policy
enforcement over branch/commit/PR/check context, recorded in an append-only
audit trail keyed to stable code identity (SEI), so it survives rename/move.

Reach for it when a policy fires at the CI/git boundary, when a change needs a
recordable override or human sign-off, or when you need git/CI context.

- Prefer the `mcp__legis__*` MCP tools; fall back to the `legis` CLI.
- Clear a fired policy through `override_submit` (MCP-only), which grades it
  (self-clear / judged / escalated) and records it — routing around it leaves
  no trail.

Full reference: the `legis-workflow` skill, `legis --help`, MCP schemas.
<!-- /legis:instructions -->

<!-- wardline:instructions:v1:bcd19330 -->
<!-- wardline:last-writer:wardline install -->
This project uses **wardline** as its trust-boundary gate. Before handing back code that touches external input, run `.venv/bin/python scripts/wardline_gate.py` (exit 0 = clean and non-inert, 1 = active ERROR findings or an inert/zero-boundary gate, 2 = Wardline/configuration error) and fix genuine findings at the boundary, not the sink. The project command supplies the exact trust grants for ELSPETH's local vocabulary pack and rejects Wardline's otherwise-green inert posture; a bare `wardline scan` is not this project's gate. The full scan -> explain -> fix -> rescan loop and the baseline-vs-waiver discipline live in the `wardline-gate` skill.
<!-- /wardline:instructions -->

## Judge-signature stage (tier-model allowlist signing)

The `trust_tier.tier_model` lint allowlist seals each judge-gated suppression with an operator-held HMAC signature. Acquiring, repairing, or rotating those signatures runs across a two-actor seam: an agent **stages** a worklist key-free via the `elspeth-judge` MCP server (`mcp__elspeth-judge__*`: `stage_scan` / `stage_status` / `verify_signatures` / `stage_preview` / `stage_rekey`), and the **operator** fires it with the key via the `elspeth-lints` CLI (`sign-bundle` / `rekey`). **Staging asserts; firing verifies** — the operator step re-derives every binding from the live tree and aborts before any write on staleness. An agent must NEVER hold `ELSPETH_JUDGE_METADATA_HMAC_KEY` (the [O1] custody rule, elspeth-b3a3335c9f) and signing never runs in CI. Do not hand-edit a `judge_metadata_signature` or resurrect the old per-release signing runbooks — stage a bundle and have the operator fire it. All judging — including the final signature verdict — runs on the agentic harness with read-only tool access (`--judge-transport agent --judge-tools readonly`, 2026-07-09 policy): the judge explores the tree before ruling, and its rationale is secret-scrubbed before persist. The full workflow lives in the `judge-signature-workflow` skill and `docs/judge-signature-handoff.md`.
