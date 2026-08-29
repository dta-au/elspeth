# ADR-044: Retire Legis — the elspeth-judge Seam Is the Governance Layer

**Date:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** trust-tier, gates, tooling, governance, related-adr-043

## Context

Legis is the Weft suite's git/CI governance layer: graded policy cells
(chill / coached / structured / protected), an LLM "judge wall" that grades
overrides, HMAC-signed verdicts bound to a file fingerprint and AST path,
operator escalation, and an append-only audit trail keyed to stable entity
identity. It entered this repository in the same tooling sweep as Wardline
([ADR-043](043-retire-wardline.md)) — via its installer, not a decision.

Measured on 2026-08-29:

- `git grep -i legis` across `src`, `elspeth-lints`, `tests`, `scripts`,
  `config`: 0 hits. No CI workflow, pre-commit hook, or Python dependency.
- `.weft/legis/legis-governance.db`: one `audit_log` table, **0 rows**.
  `legis-posture.db`: no tables. No `cells.toml` was ever written, so every
  policy default-routed ("cells config: absent" in the session hook).
- 0 tickets, 0 handovers, 0 plans or specs mention it; none of its 27 MCP
  tools appears in any repository artifact.

Every capability Legis describes already exists here as a first-party,
project-specific mechanism: the judge-signature stage (`elspeth-judge`
`stage_*` MCP tools, `elspeth-lints sign-bundle`), verdicts bound to
`scope_fingerprint` + `ast_path` and sealed by an operator-held HMAC under
the [O1] custody rule, the codex-cli read-only judge, and the CI gates in
`.github/workflows/`. Legis would have been a second, generic, unconfigured
copy of that seam.

## Decision

Remove Legis: the `.mcp.json` server, the SessionStart hook, the `AGENTS.md`
installer block, both `legis-workflow` skill copies, and the `.gitignore`
entries. A guidance test pins the absence of each surface.

## Consequences

- Nothing is lost: no policy, override, attestation, or audit row was ever
  produced.
- Governance of tier-model suppressions remains exactly where it was — the
  operator-signed allowlist and its judge stage
  (`docs/judge-signature-handoff.md`).
- Sibling installer-owned skills (Loomweave, Warpline) still mention Legis as
  an optional federation peer; that text is theirs and is left alone.
