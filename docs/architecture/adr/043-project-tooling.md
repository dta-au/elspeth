# ADR-043: Project Tooling — Filigree and Loomweave Only; Everything Else Ruled Out Until Superseded

**Date:** 2026-08-29
**Status:** Accepted (Loomweave section provisional — see §Loomweave)
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

Exactly two tools carry standing agent instructions in `AGENTS.md`:

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

*Provisional:* a large-file extraction defect made Loomweave's answers
unreliable through 2026-08 (missing entities; "zero callers" returned as if
authoritative — see the "zero callers = no evidence" trap in
`docs/agents/recent-code-hints.md`), and agents measurably stopped using it.
The fix landed on 2026-08-29 with refinement continuing. A utility
evaluation on the repaired extractor — usage forensics from session
transcripts, a live capability probe against `git grep`/`git log -L`/`ast`
ground truth across file sizes, and a cost/reliability profile — is in
progress; this section is confirmed or Loomweave is retired by an amendment
to this ADR recording its result.

### Everything else is ruled out

No other tool may carry standing agent instructions, an MCP server entry in
`.mcp.json`, a hook in `.claude/settings.json` or `.git/hooks/`, a skill
copy, or a configuration file in this repository unless an ADR that
supersedes or amends this one names it and records why. This explicitly
includes the three tools retired on 2026-08-29 — **Wardline, Legis, and
Warpline** — and any future tool that a sibling installer offers to add.

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
