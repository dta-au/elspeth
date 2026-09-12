# ELSPETH

All shared agent context for this repository lives in AGENTS.md — the single
harness-neutral covenant for Claude Code, Codex, and any other agent. It
covers project orientation, quick-reference commands, gotchas, working-directory
discipline, measured-claims and test-verification policy, subagent reporting,
editing and commit hygiene, scope discipline and delivery posture, the composer
invariants, and the judge-signature stage.

The maintainer's own agent toolchain (issue tracker, code map, delegation
conventions) is described in docs/maintainer/toolchain.md; none of it is
required to contribute.

Three tasks have a canonical script and must not be improvised from raw git
or pytest commands (see AGENTS.md § Canonical scripts). Each is dry-run by
default and acts only with `--execute`:

- worktree cleanup — `scripts/worktree-cleanup.sh`
- branch safety verification before a commit, rebase, merge or push —
  `scripts/branch-safety-check.sh`
- the full-suite pre-merge gate — `scripts/full-suite-gate.sh --execute --detach`

@AGENTS.md
