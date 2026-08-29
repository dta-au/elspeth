# ADR-043: Retire Wardline — elspeth-lints Is the Sole Trust-Boundary Gate

**Date:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** trust-tier, gates, tooling, security

## Context

Wardline is a whole-program taint analyzer from the Weft suite. It entered
this repository as a side effect of a Loomweave upgrade (its installer wrote
`weft.toml`, an `.mcp.json` server, a `wardline-gate` skill in two locations,
and an instruction block in `AGENTS.md`), not by a decision recorded anywhere.
A small ELSPETH-owned grammar pack (`scripts/wardline_pack.py`) mapped the
project's own `@trust_boundary` / `@observation_boundary` decorators
(`src/elspeth/contracts/trust_boundary.py`) onto Wardline's vocabulary so the
tool could recognise the project's boundaries.

What it actually produced, measured across every scan retained locally
(18 scans, 2026-07 → 2026-08-29):

| Rule | Count | Nature |
|---|---|---|
| `WLN-L3-LOW-RESOLUTION` | 125,428 | INFO — call could not be resolved |
| `WLN-ENGINE-UNKNOWN-IMPORT` | 13,139 | engine could not resolve `typer`, `yaml`, `pydantic`, `sqlalchemy`, … |
| `PY-WL-102` | 115 | boundary-shape check; every active finding a confirmed false positive |
| `PY-WL-101` (taint: untrusted → trusted sink) | 0 | never fired |

`PY-WL-101` — the capability that would have been genuinely beyond
`elspeth-lints` — could never fire: the pack declared no sinks (`rules=()`)
and no sink marker exists anywhere in `src/`. A taint analyzer with sources
but no sinks reports where untrusted data enters and never where it must not
go. The one rule that did run, `PY-WL-102`, duplicated the
`elspeth-lints` `trust_boundary.tests` honesty gate with less information:
Wardline dispatches on decorator identity and cannot see the
`non_raising=True` keyword that `elspeth-lints` enforces mechanically, so
every non-raising boundary manufactured a false positive
(elspeth-2a4f2fd48b).

Wardline never enforced anything here. Its pre-commit hook was removed on
2026-07-03 after an RCA of misattributed "modified by hook" failures; it was
never wired into CI; `elspeth-lints` has no code dependency on it.

## Decision

Remove Wardline from the project entirely: `weft.toml`, the `.mcp.json`
server, `scripts/wardline_pack.py`, both `wardline-gate` skill copies, the
`AGENTS.md` installer block and invocation section, the `.gitignore` entries,
and the standing instruction to run `wardline scan` before handoff. A guidance
test (`tests/unit/docs/test_project_agent_guidance.py`) pins the absence of
each surface so a future sibling-tool upgrade cannot silently re-add them.

`elspeth-lints` is the sole consumer of `@trust_boundary` metadata and the
sole trust-boundary gate: `trust_boundary.tests` (a boundary's test must exist,
raise, and reach the symbol via `source_param`; `non_raising` boundaries must
prove no raise depends on a `source_param` guard), `trust_boundary.scope`,
`trust_boundary.tier`, and the masquerade gate.

## Consequences

- No measured coverage is lost: no true-positive Wardline finding exists in
  the project's history, and every check it ran is covered by `elspeth-lints`.
- Interprocedural taint tracking is not available. It was not available
  before either — but the *option* is now a deliberate future decision rather
  than a dormant installation. If a proof that raw external data cannot reach
  secret wiring, prompt assembly, or Landscape writes becomes a requirement,
  the first step is a sink model (which producers are trusted, and why), not a
  tool. That design belongs in its own ADR.
- Historical plans, specs, sweep ledgers, and signed allowlist rationales
  that mention Wardline are left as written; they record what was true at the
  time. Signed `judge_metadata` entries are never hand-edited.
- The `_wardline_state` fixture name in composer tests refers to the
  `wardline.dev` example site used as scrape-pipeline test data, not to the
  tool, and is unchanged.
- elspeth-2a4f2fd48b (pack marker for `non_raising=True`) is closed as moot.
