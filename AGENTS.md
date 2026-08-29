# ELSPETH — Agent Guide

ELSPETH (Extensible Layered Secure Pipeline Engine for Transformation and
Handling) is a pipeline engine for building, validating, running, and auditing
LLM/data workflows whose outputs must be reviewed, explained, and reproduced.
Two authoring surfaces — version-controlled YAML and the authenticated Web
Composer (an LLM tool loop) — target one runtime model: the same plugin
contracts, graph validation, executor, Landscape audit trail, and run
accounting. Validation and audit are part of the workflow, not after-the-fact
diagnostics.

This file is the harness-neutral covenant for any agent or contributor. The
maintainer's own agent toolchain (issue tracker, code map, delegation
conventions) is described in [docs/maintainer/toolchain.md](docs/maintainer/toolchain.md);
none of it is required to contribute.

## Quick reference

```bash
source .venv/bin/activate      # uv-managed venv (Python 3.12+)
pytest tests/                  # full suite; the plain default selection IS the CI-equivalent run
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth   # static-analysis / trust-tier lint gate
elspeth run --settings examples/<name>/settings.yaml --execute
```

## Gotchas

- **STOP — read [CONTRIBUTING.md § Whole-tree gates](CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit)
  BEFORE writing code. This is not optional.** Whole-tree AST gates pin the
  EXACT set of dynamic-attribute sites, masquerade sites (tests included),
  wire-shape templates, and output bytes; a locally green scoped run proves
  nothing about them, and one careless `getattr` turns the branch red for
  every sibling (this has happened — 7201beeb7). Dated incident log:
  [docs/agents/recent-code-hints.md](docs/agents/recent-code-hints.md).
- Scoped test runs miss cross-cutting gates — run the full `pytest tests/`
  before merging.
- `elspeth-lints check` requires an explicit `--rules` selection and exits 2
  without one (until 2026-08-07 the bare command ran zero rules and exited 0 —
  a green that certified any tree); scope `--root src/elspeth` so whole-repo
  rules do not walk `.venv`. The `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE`
  prefix is what lets a keyless agent run it at all: verification otherwise
  demands `ELSPETH_JUDGE_METADATA_HMAC_KEY`, which agents must never hold
  ([O1]). Shape-only verification cannot detect forged judge metadata, so a
  trusted context must re-verify before any merge is authoritative — the same
  treatment CI gives fork PRs.
- That gate currently exits 1 with a large finding corpus: the deliberate
  fail-closed state described under "Judge-signature stage", not a regression
  you introduced. Compare the corpus before and after your change, not to zero.
- Treat the trust-tier gate as a catch-obvious-bug-hiding check, not a death
  pact. Review every touched file in full; apply the trust-tier rules to
  production code and clean related tests, config, and docs to house style.
  Use approved boundary metadata only for honest Tier-3 parsing. If one narrow
  finding is genuinely policy-wrong, keep the clearest correct code and leave
  it ready for adjudication. Never add aliases, padding, reordering, dead code,
  or semantic distortion merely to reduce signature churn — binding churn is an
  honest release obligation. Never hand-edit signatures: agents leave or stage
  key-free work and the operator signs when the package or release is complete.
- Validate by trust domain ([ADR-032](docs/architecture/adr/032-validate-by-trust-domain.md)):
  nominally type what ELSPETH owns (`isinstance` against a concrete class we
  define), parse what it does not (sentinel `getattr` + value assertions +
  construct an owned type). Never use a `runtime_checkable` Protocol as a
  security or dispatch control: it is structural typing, so an impostor
  passes, widening it silently reclassifies every implementation tree-wide,
  and since Python 3.12 it rejects dynamic-attribute objects such as pydantic
  `extra="allow"` models.
- Worktrees live under `.claude/worktrees/<name>` and symlink `.venv` to the
  main checkout: a bare `uv pip install` inside one clobbers the main venv, and
  a bare `python`/`pytest` silently imports the MAIN checkout's `elspeth`
  (editable install). `elspeth_lints` lives in a separate source root
  (`elspeth-lints/src/`), so `<worktree>/src` alone still measures the main
  checkout's `elspeth_lints`. Either way is a *confidently wrong* answer, not
  an error. Put BOTH roots on the path and verify `elspeth.__file__` and
  `elspeth_lints.__file__` point into the worktree (the `elspeth-lints`
  console script hardcodes the main venv's interpreter and only honours a
  `PYTHONPATH` the parent exports):

  ```bash
  PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src \
    <venv>/bin/python -m pytest ...
  ```

- Do not silently switch a shared checkout onto a task branch; prefer a
  dedicated worktree for branch-scoped work and surface the choice first if
  you must switch. In a shared checkout, stage only your own pathspecs and
  never `git restore`/`clean` files you did not stage.
- The pre-commit secret scanner rescans every line of a touched file, so old
  lines can fire on unrelated edits. Append `# secret-scan: allow-this-line`
  to a false positive; do not bypass the hook with `--no-verify`.
- `git stash` is blocked by a hook — use worktrees or commits instead.
- Never commit a `/home/<user>` or `/Users/<user>` path in a tracked file:
  hooks bind to `${CLAUDE_PROJECT_DIR}`, skills resolve the checkout with
  `git rev-parse --show-toplevel`, and tests pin both.
- `AGENTS.md` and `CLAUDE.md` are tracked (since 2026-07-28) so fresh
  worktrees inherit them; commit edits like any other file and review
  installer-written diffs before staging.
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

Audit grade is a characteristic of the product, not of the project's own
tooling ([ADR-046](docs/architecture/adr/046-audit-grade-is-a-product-characteristic.md)).
Issue trackers, code maps, hooks, scan stores, and installed helpers get
ordinary hygiene: purge, delete, reset, or uninstall with the tool's own verbs
(or direct SQL/`rm` when it has none) and report it — no status-semantics
debates for tool-internal rows, no ADR for a cache, no backups staged as
evidence. Destructive shared-state actions still get an operator go-ahead;
what is removed is the ceremony, not the check. Adding or removing a tool that
carries standing agent instructions is still a recorded decision
([ADR-043](docs/architecture/adr/043-project-tooling.md)).

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

The `trust_tier.tier_model` lint allowlist seals each judge-gated suppression with an operator-held HMAC signature. Acquiring, repairing, or rotating those signatures runs across a two-actor seam: an agent **stages** a worklist key-free via the `elspeth-judge` MCP server (`mcp__elspeth-judge__*`: `stage_scan` / `stage_status` / `stage_annotate` / `verify_signatures` / `stage_preview` / `stage_rekey`), and the **operator** fires it with the key via the `elspeth-lints` CLI (`sign-bundle` / `rekey`). **Staging asserts; firing verifies** — the operator step re-derives every binding from the live tree and aborts before any write on staleness. An agent must NEVER hold `ELSPETH_JUDGE_METADATA_HMAC_KEY` (the [O1] custody rule) and signing never runs in CI. Do not hand-edit a `judge_metadata_signature` or resurrect the old per-release signing runbooks — stage a bundle and have the operator fire it. All judging — including the final signature verdict — runs with read-only judge tool access (`--judge-tools readonly`) on whichever `--judge-transport` the operator selects: the judge explores the tree before ruling, and its rationale is secret-scrubbed before persist. The full workflow lives in the `judge-signature-workflow` skill and [docs/judge-signature-handoff.md](docs/judge-signature-handoff.md).
