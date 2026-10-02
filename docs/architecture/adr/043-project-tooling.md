# ADR-043: Shared Development Tooling and GitHub Issues

**Date:** 2026-08-29
**Amended:** 2026-09-25
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** tooling, delivery-posture, trust-tier, gates, related-adr-046

## Context

The project is moving from a maintainer-local issue database and code index
to shared development configuration and GitHub Issues. Tool installers had
written standing instructions, MCP registrations, skills, and lifecycle hooks
into the project. Those integrations must follow an explicit maintainer
decision rather than whichever installer ran most recently.

## Decision

### Shared issue tracking

GitHub Issues is the shared system of record. The maintainer will triage and
upload the remaining archived issues in a separate effort. Retiring local
tools does not authorize automatic publication of their backlog.

Legacy `elspeth-*` identifiers in comments and historical records remain
archive references; they must not be treated as GitHub issue numbers.
Original documents, full issue exports, and local database snapshots are
preserved in a private, gitignored archive. Historical tool names and obsolete
instructions are removed from the working tree at the maintainer's request.

### Source navigation and personal tools

Use source inspection, `rg`, language navigation, and Git history for code
structure and callers. The project requires no local code-index service.
User-wide binaries may remain for other projects. Editors and agent harnesses
are contributor choices; the shared configuration must not depend on a
maintainer's private tracker, index, credentials, or home directory.

### Baseline toolchain and product gates

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
The first-party elspeth-lints package is *product-grade*
under [ADR-046](046-audit-grade-is-a-product-characteristic.md): its gates,
allowlists and signatures protect code and releases. Reference material:
[ADR-023](023-custom-python-ci-analyzer.md) (why a custom analyzer),
[`elspeth-lints/README.md`](../../../elspeth-lints/README.md) (rule
coverage and local usage), and `docs/elspeth-lints/` —
[rationale](../../elspeth-lints/rationale.md),
[protocols](../../elspeth-lints/protocols.md),
[rule author guide](../../elspeth-lints/rule-author-guide.md),
[static/runtime boundary](../../elspeth-lints/static-runtime-boundary.md).
New static-analysis gates belong in elspeth-lints.

### Integration changes are recorded decisions

Adding an integration that carries standing agent instructions, MCP entries,
hooks, skills, or configuration requires a recorded decision describing its
benefit. Review installer-written changes as ordinary repository changes.

Product tests do not pin developer tooling choices, installations, local hooks,
skill copies, or standing-instruction wording (operator ruling, 2026-09-10).
This does not remove checks protecting runtime behavior, code, releases,
exports, or user data. See [ADR-046](046-audit-grade-is-a-product-characteristic.md).

Retirement uses ordinary hygiene: archive useful state, remove the project's
integration surfaces and local data, and report the result. Destructive
shared-state actions still need the maintainer's authorization.

## Retirements

### Local issue tracker and code index — retired 2026-09-25

The maintainer explicitly directed archive and removal, including historical
mentions, while keeping shared binaries installed. Remove project MCP entries,
session and Git hooks, tool skills, configuration, and local stores. Retain
unrelated hooks and product tools. Automated review findings stay local for
triage; an explicitly invoked issue workflow may use GitHub Issues.

The original historical documents and tool state are private migration
material. No issue upload or GitHub mutation is part of this retirement.

### Wardline — retired 2026-08-29

The third-party taint analyzer did not provide a demonstrated result beyond
the first-party boundary gates. Its project integrations and local state were
removed. Trust-boundary honesty remains enforced by elspeth-lints. Any future
interprocedural taint analysis must start with an explicit sink model and a
new recorded decision.

### Legis — retired 2026-08-29

The external governance layer duplicated the first-party judge-signature seam.
Its project integrations and local state were removed. Operator key custody
and signed code/release admission remain product safeguards.

### Warpline — retired 2026-08-29

The temporal impact tool did not reliably capture worktree-authored commits
and merges. Its integrations and local state were removed. Git history and
source inspection support impact analysis; choose tests by the reach of the
change under CONTRIBUTING.md.

### prove-it — retired 2026-09-13

The maintainer removed the optional completion-claim verifier, hook, skills,
and local state. The lane-manager pytest helper remains independently owned
by lane-manager. No replacement completion gate was introduced.

## Consequences

- Shared work is visible through GitHub Issues; backlog publication follows
  maintainer triage.
- Local archives preserve migration material without adding private state to
  the repository.
- First-party lint, contract, trust-tier, and judge-signature safeguards remain.
- Contributor tool preferences do not become product installation requirements.
