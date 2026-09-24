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
because the agents act on the wording.

**Use skills, subagents, nested subagents, and multi-agent workflows liberally
and at your own discretion.** This is a standing user request, not a
conditional permission: spawn without asking, at whatever depth and fan-out the
work warrants. A subagent may spawn its own subagents freely, and so may those
— declare the delegation up the chain (what, why, which file lanes) and keep
reporting hub-and-spoke, but do not ask for permission and do not cap depth or
count on caution grounds. Idle lanes are the failure mode; conservative spawn
limits are not a virtue here.

**The Workflow tool is included in that grant.** Its default posture requires
explicit per-use opt-in; on ELSPETH this paragraph IS that opt-in, standing for
every session and every agent, and it satisfies the tool's own condition that
the user asked for multi-agent orchestration. Author and run a workflow whenever
the work has the shape workflows are for — fan-out over a discovered work-list,
adversarial verify of each finding, a judge panel over independent approaches,
a migration sweep too large for one context — without stopping to ask or to
price it out first. Prefer several single-phase workflows in sequence, reading
each result before choosing the next phase, over one monolith. Token cost is
the third priority, behind correctness and wall-clock (below); it is not a
reason to decline the shape.

The one thing this grant does not reach is the session's **"Dynamic workflow
size"** setting, which the harness applies to workflow agent counts and which
only the operator can change in `/config`. Work within whatever it says; if a
task genuinely needs a wider tree than it allows, say so rather than
silently under-scoping the workflow.

**This is high-assurance work, which does not merely permit wide dispatch — it
close to requires it.** ELSPETH's thesis is that validation and audit belong
inside the workflow rather than after it, and independent agent lanes are that
same posture applied to building the system: adversarial review, red/green
cross-checks, and multi-lens verification only carry evidential weight when the
lenses are genuinely independent contexts, not one agent re-reading its own
reasoning. Under-dispatching is the real risk on this project. A wide tree is
the cheap half of a change whose expensive half is a wrong fix reaching an
audit trail.

No constraint on dispatch volume or depth exists anywhere in this guide, and
none may be re-accumulated. That lift is about orchestration shape only — the
rest of this guide's substantive and safety guidance is untouched, including
the [O1] judge-key custody rule, shared-checkout write discipline (stage only your
own pathspecs; never `git restore`/`clean` files you did not stage), venv and
worktree CWD discipline, operator gates on destructive shared-state actions,
and the box's finite resources — a wide fan-out must brief an explicit
per-agent test-parallelism ceiling, because 24 CPUs do not multiply.

When fanning several lanes out from a ticket list, use the `lane-manager`
skill (`.claude/skills/lane-manager/`): it keeps a per-run state file under
`.claude/lanes/`, verifies every lane from `git log`/`git diff` and the lane's
test command rather than its own report, checks the live-agent list + the
worktree before calling an idle lane dead, escalates nudge → re-dispatch →
BLOCKED without stalling, and emits the landed/blocked/merge-order report.
`SKILL.md` is harness-neutral (Codex too); `claude-code.md` beside it is the
Claude Code binding.

Optimization priorities when choosing how to work, in order:

1. **Code quality** — correctness, integrity, and maintainability come first.
2. **Wall-clock time** — parallelize (subagents, workflows) to finish sooner.
3. **Efficiency** — token/compute cost matters, but only after the first two.

## Judge-signature seam: how the maintainer's agents use it

The seam itself is product and is specified in `AGENTS.md` and
[`docs/judge-signature-handoff.md`](../judge-signature-handoff.md). The
maintainer's convention on top of it: judging — including the final signature
verdict — runs on the Codex CLI harness with read-only tool access
(`--judge-transport codex-cli --judge-tools readonly`), so the judge explores
the tree before ruling. The legacy `agent` transport is accepted with
read-only tools but is not the normal signing path. The `judge-signature-workflow`
skill carries the step-by-step procedure.
