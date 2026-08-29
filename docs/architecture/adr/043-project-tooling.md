# ADR-043: Project Tooling — Filigree and Loomweave Are the Maintainer's Integrations; the Project Ships No Others

**Date:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** tooling, delivery-posture, trust-tier, gates, related-adr-046

## Context

ELSPETH is developed with help from agent-facing tooling: an issue tracker
whose state agents read and write, and a code map agents query instead of
grepping. Each tool ships an installer that writes its own block into
`AGENTS.md`, drops skill copies under `.claude/skills/` and `.agents/skills/`,
registers an MCP server in `.mcp.json`, and may add session or git hooks.
Because the standing instructions in `AGENTS.md` are what every agent acts
on, **whatever the installers write becomes project policy by default.**

That is how three tools from the same suite — Wardline, Legis, Warpline —
entered this repository in 2026-06/07 without a decision: a Loomweave
upgrade ran their installers. By 2026-08-29 all three carried standing
instructions, MCP servers, hooks, and hundreds of megabytes of local state,
and none had produced a result that changed a decision. The evidence is
recorded in §Retirements below.

This ADR exists so that the set of tools agents are told to use is a
*decision*, recorded once, rather than the residue of whichever installer
last ran.

## Decision

### Approved tooling

Beneath everything sits the baseline Python toolchain, and above it three
tools carry standing agent instructions: one first-party (in `AGENTS.md`),
two third-party (in `docs/maintainer/toolchain.md`, since the 2026-08-30
amendment below).

**Baseline toolchain — ruff and mypy** (with `uv`, `pytest`, and
`pre-commit` as their carriers). Both are pinned as dev dependencies in
`pyproject.toml` (`ruff==0.15.4`, `mypy>=1.20,<2`) and configured there
only: `[tool.ruff]` (target `py313`, line length 140, `src`/`tests`/
`elspeth-lints/src` roots, lint-rule test fixtures excluded so ruff cannot
"fix" a deliberately malformed fixture) and `[tool.mypy]` (`strict = true`,
`warn_unreachable`, `warn_unused_ignores`, the pydantic plugin, fixtures and
`examples/` excluded). They run in three places that must agree: the
`ruff` / `ruff-format --check` / `mypy` pre-commit hooks on changed files
(check-only — hooks never rewrite files), CI's lint job
(`ruff check` and `ruff format --check` over `src/ tests/ scripts/ examples/
elspeth-lints/src/`; `mypy src/ elspeth-lints/src/`), and by hand from the
venv. They are the generic layer: anything ELSPETH-specific that ruff or
mypy cannot express is an elspeth-lints rule, not a ruff plugin, a mypy
plugin, or another tool. Adding a third-party linter, formatter, or type
checker alongside them is covered by the exclusion rule below.

**elspeth-lints** (first-party) — the project's own static-analysis and gate
platform, an internal monorepo package at `elspeth-lints/` (not a PyPI
distribution). It is the single home for every ELSPETH-specific CI/CD
invariant: the trust-tier model and trust-boundary honesty gates
(`trust_tier.*`, `trust_boundary.*`, the masquerade gate), plugin and
composer contracts, contract invariants, immutability, audit-evidence and
manifest rules, and the meta-rule `meta_no_new_bespoke_cicd_enforcer`, which
forbids new bespoke enforcers outside the package — so "add a gate" always
means "add an elspeth-lints rule", never "add a tool". It runs as
per-rule pre-commit hooks (`elspeth-lints-*` in `.pre-commit-config.yaml`),
in CI (`.github/workflows/ci.yaml`,
`enforce-allowlist-judge-gates.yaml`), and by hand
(`elspeth-lints check --rules … --root src/elspeth`; the `--rules` selection
is mandatory and `--fail-on-inert` guards against a scan that checked
nothing). Its judge-signature stage is exposed to agents as the
`elspeth-judge` MCP server in `.mcp.json` and to the operator as
`elspeth-lints sign-bundle` / `rekey`, across the two-actor seam described in
`docs/judge-signature-handoff.md` and the `judge-signature-workflow` skill.
Unlike the two third-party tools below, elspeth-lints is *product-grade*
under [ADR-046](046-audit-grade-is-a-product-characteristic.md): its gates,
allowlists and signatures protect code and releases. Reference material:
[ADR-023](023-custom-python-ci-analyzer.md) (why a custom analyzer),
[`elspeth-lints/README.md`](../../../elspeth-lints/README.md) (rule
coverage and local usage), and `docs/elspeth-lints/` —
[rationale](../../elspeth-lints/rationale.md),
[protocols](../../elspeth-lints/protocols.md),
[rule author guide](../../elspeth-lints/rule-author-guide.md),
[static/runtime boundary](../../elspeth-lints/static-runtime-boundary.md).
The exclusion rule below does not constrain elspeth-lints: it is the
mechanism through which the project adds analysis, and it is what made the
three retired third-party analyzers redundant.

**Filigree** — the project's issue tracker and work-claim authority. Data in
`.weft/filigree/` (untracked). Surface: `mcp__filigree__*` MCP server, the
`filigree` CLI, a SessionStart hook that prints the project snapshot, the
installer block in `AGENTS.md`, and the `filigree-workflow` skill. Agents use
it to find, claim, comment on, and close work; lane deliverables are tracker
writes. Its file registry and scanner subsystem are present but unused
(`registry_backend: local`; no scanner has ever run) and are not part of the
approved surface — do not enable the Loomweave registry displacement or
Loomweave→Filigree finding emission without a superseding ADR (measured
2026-08-29: the emission produced nothing, and the only finding source was
Wardline).

**Loomweave** — the code-structure map: entities with Stable Entity
Identity (SEI), call/reference/import/relation edges, subsystems, semantic
search. Index in `.weft/loomweave/` (untracked); re-analysis is triggered by
Loomweave-managed `post-commit` / `post-merge` / `post-checkout` blocks in
`.git/hooks/` and reported by a SessionStart hook. Surface: `mcp__loomweave__*`
MCP server, the `loomweave` CLI, the installer block in `AGENTS.md`, and the
`loomweave-workflow` skill (its `SKILL.md` and fingerprint are gitignored and
installer-managed). Agents use it for "where is X / who calls X / what
subclasses X / find the thing that does Y". Its Filigree issue lookup
(`integrations.filigree.enabled: true` in the untracked `loomweave.yaml`) is
on and works; the 28 issue↔entity bindings it can see were written as
side-effects and have never driven a decision — they are tolerated, not
relied on.

*Scope of approval.* Loomweave is approved for the questions it measured
correct on (2026-08-29, live probe against `ast`/`git` ground truth):
**callers of an entity** (precision/recall 1.0 on every `src/` case probed,
including a function-local-import caller), **subclasses / relations** (exact,
where grep returns docstring false positives), **execution paths / call
trees** (no grep equivalent), and **definition lookup in large files**. It is
*not* advertised for semantic search, dead-code candidates, HTTP-route
inventories, or test-caller lists until the defects recorded in
`docs/superpowers/plans/2026-08-29-loomweave-salvage-worklist.md` are fixed
— on those questions `git grep` measured as good or better, and a tool that
is right on three questions and wrong on four teaches agents to distrust all
seven.

*Why it is kept rather than retired.* A large-file resolution defect degraded
97–100 % of analyzed files through 2026-08 (five consecutive failed runs in
the two hours before the fix); agents measurably stopped using it (Codex:
1,970 calls in July, 1,882 of them cancelled over two days, then 164 in
August; Claude Code: 104 of 180,517 tool calls, none from search-specialist
or lane agents). The agents' own recorded reason was less "wrong answer"
than "trust question": stale-index warnings (38 explicit statements across
both harnesses) and the rule that a zero-caller answer must be corroborated
by grep — "once I'm going to grep, I skip the first step." The extractor fix landed 2026-08-29 17:50 local; the first post-fix
incremental run degraded 3 of 45 files, and the first post-fix full
re-analyze (run `f085278d`, 38 min, all 3,093 files, 0 skipped) degraded
**3 files in the whole tree** — `web/sessions/service.py` (13,645 lines,
reference-site cap), `web/composer/tool_batch.py` (2,354 lines, pyright
timeout), and one 1,165-line integration test — with zero pyright restarts. The maintainers' preference is to
salvage: the graph is theirs, the three approved capabilities have no
substitute, and the remaining problems are enumerable. The salvage worklist
is the condition of this approval; the measurement that decides whether it
worked is adoption, re-run with the same transcript forensics after the next
delivery wave. If lane and Explore agents still make zero Loomweave calls
against a working index, this section is amended to a retirement.

### The project ships no tool integrations beyond these

The project ships no tool integrations beyond the ones named above;
contributors use whatever tools they like. What this rules out is the
*repository* carrying an integration — standing agent instructions, an MCP
server entry in `.mcp.json`, a hook in `.claude/settings.json` or
`.git/hooks/`, a skill copy, or a configuration file — for any other tool
unless an ADR that supersedes or amends this one names it and records why.
This explicitly includes the three tools retired on 2026-08-29 — **Wardline,
Legis, and Warpline** — and any future tool that a sibling installer offers
to add. It says nothing about what a contributor runs locally.

Mechanism: `tests/unit/docs/test_project_agent_guidance.py` pins the
*absence* of every installer-written surface for each excluded tool
(`AGENTS.md` block markers, `mcp__<tool>__` mentions, `.mcp.json` server,
hook commands, skill directories, pack files). A re-leak fails CI instead of
becoming a standing instruction. Proposing a new tool means: write the
superseding ADR with a measured case (what it answers that the approved
tools and git do not), add its surfaces, and move its absence test to a
presence test in the same change.

Retiring a tool is the reverse and, per [ADR-046](046-audit-grade-is-a-product-characteristic.md),
gets ordinary hygiene rather than audit-grade ceremony: remove the surfaces,
add the absence test, purge its local state and its footprint in sibling
tools, record the measurement here.

## Retirements (evidence)

### Wardline — retired 2026-08-29

Whole-program taint analyzer. Its pack (`scripts/wardline_pack.py`) mapped
ELSPETH's `@trust_boundary` / `@observation_boundary` decorators onto its
grammar but declared no sinks, and no sink marker existed in `src/`. Measured
across all 18 retained scans (2026-07 → 2026-08-29):

| Rule | Count | Nature |
|---|---|---|
| `WLN-L3-LOW-RESOLUTION` | 125,428 | INFO — call could not be resolved |
| `WLN-ENGINE-UNKNOWN-IMPORT` | 13,139 | could not resolve `typer`, `yaml`, `pydantic`, `sqlalchemy`, … |
| `PY-WL-102` | 115 | boundary-shape check; every active finding a confirmed false positive |
| `PY-WL-101` (taint: untrusted → trusted sink) | 0 | never fired — no sinks to flow into |

`PY-WL-102` duplicated the `elspeth-lints` `trust_boundary.tests` honesty
gate with less information (Wardline dispatches on decorator identity and
cannot see `non_raising=True`, so every non-raising boundary manufactured a
false positive — elspeth-2a4f2fd48b). Its pre-commit hook had been removed on
2026-07-03; it was never in CI; `elspeth-lints` has no code dependency on it.
Removed: `weft.toml`, `.mcp.json` server, the pack, both `wardline-gate`
skill copies, the `AGENTS.md` block and invocation section, `.gitignore`
entries, 82 MB of `.wardline/` artefacts, and 79,022 Wardline-sourced rows in
the Filigree finding store (100 % of that store). `elspeth-lints` is the sole
consumer of `@trust_boundary` metadata and the sole trust-boundary gate.
Interprocedural taint tracking, if ever needed, starts with a sink model in
its own ADR, not a tool.

### Legis — retired 2026-08-29

Git/CI governance layer: graded policy cells, an LLM judge wall, HMAC-signed
verdicts bound to file fingerprint + AST path, operator escalation, an
append-only trail keyed to SEI. Measured: 0 hits in `src`/`elspeth-lints`/
`tests`/`scripts`/`config`; `legis-governance.db` had one `audit_log` table
with **0 rows** and `legis-posture.db` no tables; no `cells.toml` was ever
written (every policy default-routed); 0 tickets, handovers, plans, or specs
mentioned it; none of its 27 MCP tools appears in any repository artefact.
Every capability it describes is a first-party mechanism of the
judge-signature seam (`elspeth-judge` `stage_*`, `elspeth-lints sign-bundle`,
verdicts bound to `scope_fingerprint` + `ast_path` under the [O1] custody
rule, codex-cli read-only judge, CI gates in `.github/workflows/`). Removed:
`.mcp.json` server, SessionStart hook, `AGENTS.md` block, both
`legis-workflow` skill copies, `.gitignore` entries, `.weft/legis/`.

### Warpline — retired 2026-08-29

Temporal change-impact tool: changed entities per revision range, impact
radius, re-verify worklist, timelines, churn. Live side-by-side probe on a
production entity changed in `HEAD~5..HEAD`
(`_replace_advisor_repair_public_result`, `web/composer/service.py`):

| Question | Warpline | Source that answered it |
|---|---|---|
| Changed in `HEAD~5..HEAD` | 3 items in 2 Markdown files | git: 14 files, 10 under `src/`/`tests/` |
| Callers of the entity | `affected: []`, snapshot 2,178 commits behind | Loomweave `entity_callers_list`: 1 resolved caller, matches `git grep` |
| Tests to re-run | 2 doc files, `priority: unknown`, no test names | `git grep` under `tests/`: 3 files |
| Timeline / churn | 41 file-touch events | `git log -L`: 7 commits to the function body |

Structural cause: its only ingestion path was an untracked
`.git/hooks/post-commit` block, so worktree-authored commits and `--no-ff`
merges — how this project lands work — were never recorded (6 of the 9 most
recent commits had zero rows). Every recorded use in the project's history
said the same ("not authoritative", "cannot safely narrow the verification
set", "1,007 commits behind"). Its federation never materialised: Loomweave's
churn views delegated to it and were disabled; Filigree ingested 0 worklists;
every enrichment field on every call was `absent`/`unavailable`/`stale`. Cost
of keeping it: a 374 MB store and a 4.3 s SessionStart hook 0.7 s under its
own timeout. Removed: `.mcp.json` server, SessionStart hook, `AGENTS.md`
block, both `warpline-workflow` skill copies, the post-commit block,
`.weft/warpline/`. Loomweave answers the caller question; git answers the
rest; the full `pytest tests/` run before merge stays the rule.

## Consequences

- The tool set agents act on is a recorded decision with a CI-enforced
  boundary; installer drift cannot silently widen it.
- New static analysis or gating goes into elspeth-lints as a rule, which
  `meta_no_new_bespoke_cicd_enforcer` already enforces; a third-party
  analyzer is only ever a candidate if it answers something elspeth-lints
  structurally cannot (the interprocedural-taint case in §Wardline), and then
  only through a superseding ADR.
- Nothing measured is lost by the three retirements: no true-positive
  Wardline finding, no Legis record, and no Warpline answer that narrowed a
  verification set exists in the project's history.
- Historical plans, specs, sweep ledgers, assessment artefacts, and signed
  allowlist rationales that mention the retired tools are left as written;
  they record what was true at the time. Signed `judge_metadata` entries are
  never hand-edited. Installer-owned text in the Filigree and Loomweave
  skills that names the retired tools as federation peers is theirs and is
  left alone.
- The `_wardline_state` fixture name in composer tests refers to the
  `wardline.dev` example site used as scrape-pipeline test data, not the
  tool.
- Binaries under `~/.local/bin` for the retired tools were uninstalled
  (`uv tool uninstall legis wardline warpline`); nothing in the repository
  references them.

## Amendment 2026-08-30: covenant / toolchain split

**Amendment Deciders:** ELSPETH maintainers

`AGENTS.md` had grown to 425 lines and blended three layers: project
invariants any contributor needs, project-specific knowledge, and the
maintainer's personal agent toolchain (Filigree, Loomweave, the lane-manager
skill, the standing delegation grant) stated as a mandate. Because
`tests/unit/docs/test_project_agent_guidance.py` failed CI whenever the docs
stopped naming those tools, the toolchain had become a product commitment —
exactly what [ADR-046](046-audit-grade-is-a-product-characteristic.md) says
project tooling must not be. ELSPETH is a public repository; a contributor
should not read a tracker or code-map choice as a condition of contributing.

Decision: split by layer.

- `AGENTS.md` is a short, harness-neutral public covenant: orientation,
  quick reference, repository gotchas, delivery posture, the composer
  invariants, and the judge-signature stage. It carries a one-paragraph
  pointer to the maintainer document and states that nothing there is
  required to contribute.
- `docs/maintainer/toolchain.md` holds everything removed: the Filigree and
  Loomweave installer blocks (the installers rewrite those blocks; they now
  live in this file), ELSPETH's own Loomweave usage notes, the standing
  authorization for skills/subagents/workflows, the lane-manager pointer,
  and the retired-tool note. It opens with a labelled disclaimer that it
  describes how the maintainer works and is not a requirement of the project.
- `elspeth-lints` stays in the covenant unchanged: it is product under
  ADR-046, and its judge-signature seam is how allowlist signatures are
  produced. The covenant describes that seam by its elspeth-lints flags
  (`--judge-tools readonly`) and no longer names a specific judge harness;
  the maintainer's harness choice is recorded in the toolchain document.
- The §"Everything else is ruled out" clause above is reworded: the project
  ships no tool integrations beyond the named ones, and contributors use
  whatever tools they like. The CI mechanism is unchanged — the guidance
  tests still pin the *absence* of retired-tool surfaces and the repository
  invariants (hooks bound to `${CLAUDE_PROJECT_DIR}`, bounded timeouts, no
  private home paths, no hook bypass) — but they no longer assert that the
  docs name any external tool.

The approved-tooling section and the retirement evidence above stand as
written; this amendment changes where the instructions live and what the
project asks of contributors, not which tools the maintainer's agents use.
