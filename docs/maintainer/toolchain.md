# Maintainer toolchain

Contributors share GitHub Issues and repository development configuration.
Personal editors, agent harnesses, and locally installed binaries remain optional.
The project covenant is [AGENTS.md](../../AGENTS.md); tooling decisions are
recorded in [ADR-043](../architecture/adr/043-project-tooling.md).

## Shared work tracking and source navigation

Use GitHub Issues for current work and link the relevant pull request and
verification results. Resolve the repository and issue number before writing;
legacy `elspeth-*` identifiers refer to the archived backlog, not GitHub numbers.
The maintainer will triage and upload that backlog separately. Automated reviews
retain local findings for review instead of publishing untriaged issues.

Use source inspection, `rg`, language navigation, and Git history to find code
and verify callers. No project-local code-index service is required.

## Standing authorization: skills, subagents, and workflows

This is the maintainer's grant to their own agents. It is reproduced verbatim
because the agents act on the wording. It was tempered on 2026-10-24
([lane-process review](lane-process-review-2026-10-24.md)): the earlier
"dispatch liberally" wording produced far more evidence than code.

**Use skills, subagents, nested subagents, and multi-agent workflows when the
work needs them.** Spawning needs no per-use permission, and a subagent may
spawn its own. Declare the delegation up the chain (what, why, which file
lanes) and keep reporting hub-and-spoke.

**Dispatch in proportion to risk.** One implementer per unit of work. One
independent review per non-trivial commit group. Add a red-team pass only on
the chokepoints AGENTS.md names (the Composer authoring and tutorial entry
path). Multi-lens verification and judge panels are for changes whose failure
would reach an audit trail or a security boundary, not for routine edits.

**Cap the evidence per unit.** A unit's evidence is its commits and the
targeted tests' fail-before/pass-after exit codes; a merge adds one
`full-suite-gate` `summary.txt`. Do not produce snapshot trees, copied stores,
envelopes, receipts or per-snapshot gate re-runs. Lane directories hold notes
and logs only (AGENTS.md § Subagent Reporting). The review measured one
six-hour window at 16,159 files and 3.22 GB of lane output against about
5.8 k lines of source and tests.

**The Workflow tool is included in that grant.** Its default posture requires
explicit per-use opt-in; on ELSPETH this paragraph IS that opt-in, standing for
every session and every agent. Use a workflow when the work has the shape
workflows are for — fan-out over a discovered work-list, adversarial verify of
each finding, a migration sweep too large for one context — and the risk
justifies it. Prefer several single-phase workflows in sequence, reading each
result before choosing the next phase, over one monolith.

The one thing this grant does not reach is the session's **"Dynamic workflow
size"** setting, which the harness applies to workflow agent counts and which
only the operator can change in `/config`. Work within whatever it says; if a
task genuinely needs a wider tree than it allows, say so rather than
silently under-scoping the workflow.

Independent review carries weight only when the reviewer is a separate
context, not one agent re-reading its own reasoning. That is the reason for
the one independent review per commit group, not a reason for volume.

Nothing in this grant relaxes the rest of this guide or AGENTS.md: the [O1]
judge-key custody rule, shared-checkout write discipline (stage only your own
pathspecs; never `git restore`/`clean` files you did not stage), venv and
worktree CWD discipline, operator gates on destructive shared-state actions,
and the box's finite resources. Brief every agent with an explicit
test-parallelism ceiling, because 24 CPUs do not multiply, and run one broad
suite at a time.

When fanning several lanes out from a ticket list, use the `lane-manager`
skill (`.claude/skills/lane-manager/`): it keeps per-run state under
`.claude/lanes/<run-id>/`, verifies every lane from git (commits ahead of
base) and a red → green run of the lane's test in throwaway worktrees rather
than from its own report, checks the live-agent list + the worktree before
calling an idle lane dead, escalates nudge → re-dispatch → BLOCKED without
stalling, and emits the landed/blocked/merge-order report. The full suite
runs once on the final merged tree, not per lane. `SKILL.md` is
harness-neutral (Codex too); `claude-code.md` beside it is the Claude Code
binding.

When choosing how to work, correctness, integrity and maintainability come
first. After that, weigh wall-clock time and token and compute cost together:
parallel work that does not shorten the critical path, and evidence nobody
will read again, are both waste.

## Judge-signature seam: how the maintainer's agents use it

The seam itself is product. Follow
[`AGENTS.md` § Operator signature verification](../../AGENTS.md#operator-signature-verification-tier-model-allowlist-signing)
and [`docs/judge-signature-handoff.md`](../judge-signature-handoff.md) for its
current custody and transport rules. Signing is suspended. Key-free judge calls
require the installed judge supervisor and the `agent` transport with read-only
tools; they refuse when the supervisor is unavailable. There is no local
Codex/SDK fallback. The `judge-signature-workflow` skill carries the procedure.
