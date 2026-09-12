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

## Working Directory Discipline

- The Bash tool persists its working directory across calls. Begin any script,
  test, or build invocation with an explicit `cd <repo-root> &&` rather than
  assuming the current directory.
- When operating in a worktree, state the worktree path in the command; never
  rely on an earlier `cd`.

## Claims Must Be Measured

- Counts, inventories, and parity checks must come from the live source of
  truth (registry, DB query, API), never from a regex over source files.
- After any status claim ("fix landed", "ticket closed", "branch merged"),
  re-verify against the current HEAD before writing it into a checkpoint or
  handoff doc.
- State no conclusion by inference — coverage gap, branch landed, root cause,
  file size, row count. Run the measuring command first and show its raw
  output beside the claim.
- Control an ad-hoc instrument before you trust a number it produced: run it
  against a known-positive case it must match and a known-negative case it
  must not. A `grep`, `find -size`, glob or regex that silently matches
  nothing returns exactly what a correct one returns when there is nothing
  to find, so a clean answer is not evidence the instrument works.
- A positive assertion over a filtered subset is still a negative claim, and
  a partial capture (`.*?` under `re.S`, `git rev-parse` echoing a missing
  path back) fails toward passing. When an instrument decides a gate, mutate
  the thing it should catch and confirm it goes red.

## Quick reference

```bash
source .venv/bin/activate      # uv-managed venv (Python 3.12+)
pytest tests/                  # default selection (~20 min at -n 12) = what CI's "Test" job runs; it EXCLUDES the PostgreSQL testcontainer suites
pytest tests/ -m testcontainer -n 0   # CI's required "Testcontainer" job (Docker, serial); never part of the default run
pytest tests/path::test -n 0   # ONE test: -n 0 disables the default 12 workers (needed for pdb / -s)
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth   # static-analysis / trust-tier lint gate
elspeth run --settings examples/<name>/settings.yaml --execute
```

### Canonical scripts: worktree cleanup, branch safety, full-suite gate

Three scripts under `scripts/` are the canonical way to do the tasks agents
otherwise improvise by hand (the header of each one cites the transcript
measurement and the incident behind every rule it enforces). All three are
**dry-run by default** and print the exact commands they would run; nothing
changes without `--execute`. Prefer them over retyping the underlying git and
pytest incantations, and fix the script when a rule is wrong rather than
working around it.

- `scripts/worktree-cleanup.sh [--execute] [--base REF] [--path GLOB] [--delete-branches] [--discard-ignored] [--link-venv]`
  classifies every registered worktree as MAIN, MISSING, LOCKED, IN-USE,
  DIRTY, UNKNOWN, UNMERGED, IGNORED or REMOVABLE; drops MISSING
  registrations and removes only clean worktrees whose HEAD is an ancestor
  of `--base` (default: the main checkout's branch). Never forces a present
  tree, never a branch with unlanded commits, never a tree whose `git status`
  failed (UNKNOWN); gitignored content beyond caches (lane notes, a local
  `.elspeth/`, `data/`) holds a tree as IGNORED until `--discard-ignored`.
  IN-USE comes from `/proc/<pid>/cwd`, not a pgrep pattern; the dry run
  writes nothing (measured: a worktree's index inode is unchanged across it).
  Note `--no-optional-locks` only silences `git status`; `git diff` still
  rewrites the stat cache, so none of these scripts diff a working tree they
  do not own. `--link-venv` repairs
  the missing `.venv` symlink that makes subprocess tests fail as ordinary
  assertion errors.
- `scripts/branch-safety-check.sh [--intent commit|rebase|merge|push] [--base REF] [--fetch]`
  is a read-only pre-flight that prints one `[PASS|WARN|FAIL]` line per check
  with the instrument that produced it: unfinished merge/rebase, protected
  branch, staged artefact paths and user-home paths, the secret scanner,
  whether HEAD is already published, ahead/behind against a **named two-dot**
  base range, rerere, the HMAC key in the environment, and that
  `elspeth.__file__` resolves inside this tree. Exit 1 on any FAIL. `--fetch`
  is its only write.
- `scripts/full-suite-gate.sh [--execute [--detach]] [--stages ruff,mypy,contracts,lints,pytest,testcontainer] [--root DIR] [--log-dir DIR]`
  runs the pre-merge gate as CI runs it: one log per stage, every exit code
  written to `summary.txt`, the tree state hashed before and after
  (`frozen=NO` means the run is not evidence), provenance proved with both
  `-o pythonpath` and an exported `PYTHONPATH`, workers capped when sibling
  suites are on the box. From an agent shell use `--execute --detach` and poll
  the printed `.done` path. The suite is never piped through `tail`.

## Gotchas

- **STOP — read [CONTRIBUTING.md § Whole-tree gates](CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit)
  BEFORE writing code. This is not optional.** Whole-tree AST gates pin the
  EXACT set of dynamic-attribute sites, masquerade sites (tests included),
  wire-shape templates, and output bytes; a locally green scoped run proves
  nothing about them, and one careless `getattr` turns the branch red for
  every sibling (this has happened — 7201beeb7). Dated incident log:
  [docs/agents/recent-code-hints.md](docs/agents/recent-code-hints.md).
- Scoped test runs miss cross-cutting gates — run the full `pytest tests/`
  before merging. `addopts` carries `-n 12`, so the bare command IS the
  parallel run: serial it is ~17 hours against 44,399 tests, which is why the
  default is parallel rather than a flag you have to remember. Pass `-n 0` for
  a single test or a debugger (`pdb` and `-s` do not work through xdist), and
  note that xdist auto-disables `pytest-benchmark` — the `performance` marker
  is deselected by default anyway.
- The default selection also deselects the `testcontainer` marker, so a green
  `pytest tests/` says NOTHING about PostgreSQL. Two 0.8.0 defects passed it:
  a one-element `IN` CHECK that PostgreSQL reflects as `=` (elspeth-d0e62aea41)
  and an SSO handoff claim race that SQLite's serialised writers cannot
  express. If you touched schema, SQL, session or Landscape persistence, or a
  lock, also run `pytest tests/ -m testcontainer -n 0` (needs Docker; serial
  because `tests/testcontainer/web/conftest.py` shares one container across the
  deployment-acceptance files and rejects xdist workers). CI runs exactly that
  selection in the required `Testcontainer (PostgreSQL contention proofs)` job
  of `.github/workflows/ci.yaml`; it is the gate for the ids the default run
  never sees.
- A handful of process-death / peer-lease / resume tests are FLAKY under
  parallelism (elspeth-0077cb7789): two runs of identical code produced
  DISJOINT failure sets, all passing serially. Before blaming your change for
  a red in `e2e/recovery`, `integration/pipeline`, or
  `unit/engine/orchestrator`, re-run the named test with `-n 0`; if it passes,
  diff your failure set against the same run on your base commit rather than
  assuming either result.
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
- Do not use `git stash` — use a worktree or a commit instead. This is a
  convention, not an enforced gate: nothing blocks it. (A `.git/hooks/pre-stash`
  once claimed to, but git has no such hook, so it never ran; it was removed
  2026-09-02 along with the claim that it worked. Blocking a stash would need a
  `reference-transaction` hook rejecting `refs/stash`, which is deliberately not
  installed — that hook fires on every ref update in the repo.)
- Never commit a `/home/<user>` or `/Users/<user>` path in a tracked file:
  hooks bind to `${CLAUDE_PROJECT_DIR}`, skills resolve the checkout with
  `git rev-parse --show-toplevel`, and tests pin both.
- `AGENTS.md` and `CLAUDE.md` are tracked (since 2026-07-28) so fresh
  worktrees inherit them; commit edits like any other file and review
  installer-written diffs before staging.
- Directory-scoped guides exist where the details live:
  `examples/AGENTS.md` (how to run every example) and
  `src/elspeth/plugins/transforms/AGENTS.md` (row data vs audit provenance).

## Test Verification Policy

- Never report test results based on `grep`, `tail`, or piped output. A pipe
  masks the exit code and buffering hides in-progress failures.
- Always run suites writing to a file, then check the exit code explicitly
  (`addopts` already carries `-n 12`, so the bare command is the parallel run):
  `pytest tests/ > "$log" 2>&1; echo "exit=$?"; tail -50 "$log"` — use a
  unique, lane-private log path, not a shared generic filename.
- Do not claim "zero failures", "suite green", or "passes in isolation" until
  the process has exited and you have read its exit code.
- `scripts/full-suite-gate.sh --execute --detach` does all of the above for
  you (per-stage logs, recorded exit codes, a frozen-tree check); read its
  `summary.txt`, not its terminal output.

## Subagent Reporting

- Brief every subagent to write its findings to a file and return only the
  path plus a short summary. The message channel truncates long reports, and
  a truncated report is indistinguishable from a complete short one.
- Coordination and lane state go under `.claude/lanes/<run>/` — the
  `lane-manager` convention, already gitignored and already named in Commit
  Hygiene as never-stage.
- A durable deliverable (a review, an inventory, a design note, an evidence
  bundle) goes to a tracked path under `docs/`. Confirm it with
  `git check-ignore -v <path>` before writing: no output means the path is
  safe. `scratch/` and `.scratch/` are ignored and have swallowed a finished
  report before.
- Dispatch to an agent type that can write. A read-only type must answer
  inline, so either scope its brief narrow enough to survive the channel or
  pick a different type.

## Editing Rules

- Do not use `sed`, `awk`, or scripted line-number rewrites to resolve merge
  conflicts or perform multi-line edits. Use the Edit tool or regenerate the
  file.
- Never add `# noqa`, `# type: ignore`, or lint suppressions to make a gate
  pass; fix the underlying issue or report it as blocked.

## Commit Hygiene

- Before every commit, run `git status --short` and confirm the staged set.
  Never stage `.claude/lanes/`, dry-run artifacts, scratch logs, or build
  output. `scripts/branch-safety-check.sh` checks the staged set for exactly
  those paths, for user-home paths, and for credential-shaped strings; run it
  before every commit, rebase or push.
- Run the lint gate (ruff) locally before pushing; do not rely on CI to
  surface unused imports or formatting.

## Scope Discipline

- The reported defect is the deliverable. Do not ship a cosmetic or adjacent
  change while deferring the actual fix into new tickets unless the developer
  approved that split first.
- If the smallest change that actually fixes the defect turns out to be out
  of scope, stop and say so plainly. Filing follow-up tickets is not a
  substitute for reporting that the fix did not land.
- Confirm the target version and branch before editing docs, changelogs or
  release notes. Which release a change belongs to is a decision to check,
  not an inference from the current checkout.
- This is not a budget cap. Wide dispatch and deep analysis are standing
  policy for the maintainer's own agents
  ([docs/maintainer/toolchain.md](docs/maintainer/toolchain.md) § Standing
  authorization); this section constrains what you hand back, not what you
  spend getting there.

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

Standing review trigger (operator ruling 2026-09-02): any latency or cost
optimization touching the rootless or tutorial entry path gets per-TRANSITION
provider-call scrutiny before it lands — both prior invariant violations
(`b073d248e`, `9700470e2`) were latency fixes on exactly this chokepoint, and
the first evaded detection for 26 days because the gate counted calls per
walk. If the trivial case feels too slow, that is a planner-brief defect to
fix (see elspeth-63cf3803e6), never a reason to route around the provider.

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
has settled. Since 2026-09-05 the `test` and `testcontainer` jobs no longer
wait on `static-analysis`, so the suites run and report while that job is red;
`CI Success` still requires `static-analysis`, so the red still blocks merges.

The `trust_tier.tier_model` lint allowlist seals each judge-gated suppression with an operator-held HMAC signature. Acquiring, repairing, or rotating those signatures runs across a two-actor seam: an agent **stages** a worklist key-free via the `elspeth-judge` MCP server (`mcp__elspeth-judge__*`: `stage_scan` / `stage_status` / `stage_annotate` / `verify_signatures` / `stage_preview` / `stage_rekey`), and the **operator** fires it with the key via the `elspeth-lints` CLI (`sign-bundle` / `rekey`). **Staging asserts; firing verifies** — the operator step re-derives every binding from the live tree and aborts before any write on staleness. An agent must NEVER hold `ELSPETH_JUDGE_METADATA_HMAC_KEY` (the [O1] custody rule) and signing never runs in CI. Do not hand-edit a `judge_metadata_signature` or resurrect the old per-release signing runbooks — stage a bundle and have the operator fire it. All judging — including the final signature verdict — runs with read-only judge tool access (`--judge-tools readonly`) on whichever `--judge-transport` the operator selects: the judge explores the tree before ruling, and its rationale is secret-scrubbed before persist. The full workflow lives in the `judge-signature-workflow` skill and [docs/judge-signature-handoff.md](docs/judge-signature-handoff.md).

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

<!-- loomweave:instructions:v1.6.0:39edbf6d -->
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
