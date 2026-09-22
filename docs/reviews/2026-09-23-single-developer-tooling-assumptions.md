# Single-developer assumptions in ELSPETH's agent tooling

**Date:** 2026-09-23
**Scope:** agent tooling and the accumulated working notes around it — the
maintainer's local memory store, `.claude/`, `.agents/`, `docs/maintainer/`,
`docs/agents/`, and the tracker/code-map workflow skills.
**Not in scope:** the repository proper (`AGENTS.md`, `CLAUDE.md`, ADRs, the
rest of `docs/`, `.github/`, `scripts/`) — a second auditor covers that. This
report cites those files only where a tooling rule contradicts them.
**Method:** read-only. No file was edited, no test was run.

## Summary

| Theme | Findings | Must change | Should change | Deliberate — keep, but write it down |
|---|---|---|---|---|
| A. Rules that exist only in personal memory | 10 | 4 | 5 | 1 |
| B. Single-holder assumptions | 6 | 3 | 1 | 2 |
| C. Machine-local coupling | 9 | 3 | 5 | 1 |
| D. Tracker coupling | 7 | 4 | 3 | 0 |
| E. Agent process vs human collaborators | 7 | 1 | 3 | 3 |
| **Total** | **39** | **15** | **17** | **7** |

The single highest-value class is **A**: operating rules a new developer would
violate on their first day, which are currently written down nowhere a
contributor can read. Ten of those are named below, four of which the project's
own tracked documents either omit or actively contradict.

### The question that decides a third of this report

**Do the incoming developers share one workstation, or does each have their
own?** I cannot answer it from the evidence, and it changes the disposition of
most of theme B and parts of C and E:

- **Separate machines.** The entire shared-checkout doctrine — commit by
  pathspec, "more than two concurrent writers means stop", "ping a lane before
  reclaiming its worktree", the per-agent test-parallelism ceiling — dissolves
  into "one checkout per person, agents get worktrees". It stops being project
  doctrine and becomes agent-harness internals, and most of it can simply be
  retired from the shared covenant.
- **A shared dev box.** Every one of those rules gets *worse*, because they are
  currently enforced by one person's discipline. A human collaborator will not
  follow a `git commit -- <pathspec>` protocol they have never been told about,
  and the failure mode (committing a colleague's in-flight work under your own
  name) is silent. On a shared box these must become mechanical: per-user
  checkouts at minimum, and a real lock on test capacity.

Findings below are written for both branches where it matters.

### A note on redaction

This is a tracked file in a public repository. Quotes from the maintainer's
local memory store are verbatim **except** that the maintainer's name is
rendered as `[the maintainer]`, and account identifiers, hostnames,
credentials, home paths and machine-local temporary paths are omitted or
described generically. One quoted expletive is elided as `[...]`.

---

## A. Operating rules that exist only in personal memory

These are the highest-value findings. Each is a rule the project actually runs
on; each lives only in the maintainer's local memory store; none is readable by
someone who clones the repository.

### A1. The branching model is premised, in writing, on there being one contributor

**Source:** memory `feedback_work_on_release_branch_not_feature_branches`.

> "we should be working on the release branch, we keep losing work to orphaned
> branches."
>
> **Why:** in this single-contributor flow, feature branches get abandoned and
> their commits are effectively lost — the branch name lingers, the work never
> merges. The release branch is the durable integration line for the version.

And its vocabulary clause:

> **Vocabulary (operator, 2026-07-17):** main only ever moves by PR, so when the
> operator says "merge to main" / "bring onto main" in conversation, they normally
> mean "land it on the current release branch" — do NOT checkout/merge/push main
> directly.

**Why it breaks:** this is the load-bearing branching rule of the project and it
names its own premise — a single contributor — as the reason it exists. With
several developers and GitHub Issues, the rule inverts: feature branches stop
being orphan risks and become the unit of review, because a pull request needs a
branch to review. "Commit directly to the release branch" with multiple people is
how you get unreviewed work on the integration line. The vocabulary clause is
worse: a shared term ("merge to main") that means something different from what
it says, known only to two parties, is a collision waiting for the first new
contributor who takes it literally.

**Recommendation — MUST CHANGE.** Decide the multi-developer branching model
explicitly (almost certainly: feature branch → PR → review → squash or merge into
the release branch; release branch → PR → `main`). Write it in `CONTRIBUTING.md`
under a "Branching and pull requests" heading. Retire the "merge to main means
land on the release branch" vocabulary entirely — under GitHub PRs the words must
mean what they say.

### A2. "Ready for merge" stops short of the merge, and release refs belong to one person

**Source:** memory `feedback_ready_for_merge_means_stop_before_landing`.

> On 2026-09-08 [the maintainer] asked me to "resolve and get it ready for merge
> following your 6 steps". [...] He interrupted within seconds: "holy [...], do
> not merge".
>
> **Why:** "ready for merge" names an END STATE short of the merge. The release
> branch is the seat's [...] to move, and the shared checkout is not mine to
> re-point.

**Why it breaks:** ref ownership is currently enforced by one person watching a
terminal and interrupting within seconds. That does not scale to a team, and it
does not scale to agents working for several people. The rule is correct; the
enforcement mechanism is a human reflex.

**Recommendation — SHOULD CHANGE, and mechanise.** Put branch protection on
`main` and the active `release/*` branch (no direct pushes, PR required). Then
the rule stops needing to be remembered: the ref physically cannot be moved by
the wrong actor. Record the intent in `CONTRIBUTING.md`; the protection rule is
the enforcement.

### A3. The definition of done lives in a tracker comment and a memory file

**Source:** memory `feedback_dod_is_merged_to_release_branch_and_green`.

> "this doesn't reduce or change the scope of work, just unblocks its closure -
> merged to release 0.8.0 and green is the new dod"

> **How to apply:** close a ticket on its landing sha on release/0.8.0 once its
> ids are green there [...]. Never close on a lane-branch sha. Do NOT trim a
> ticket's scope to meet the bar.

**Why it breaks:** a definition of done is a team contract. This one is recorded
as a comment on a milestone ticket in the tracker being migrated away from, plus
a personal memory file. After migration both homes are gone.

**Recommendation — MUST CHANGE.** Write the DoD into `CONTRIBUTING.md` and into
the GitHub issue/PR templates as a checklist ("merged to the active release
branch; the affected suites green there; closed on the landing sha, not a feature
branch sha"). It is one paragraph and it is currently the single most-cited
closure rule in the memory store.

### A4. A private grant contradicts the public covenant on `--no-verify`

**Source:** memory `feedback_no_verify_ok_with_end_of_slice_reconciliation`
versus tracked `AGENTS.md`.

Memory:

> **Strengthened to default (operator, 2026-07-04):** `--no-verify` is now the
> DEFAULT for all commits — controller AND subagents, routinely, not just when a
> hook blocks. Reconciliation is batched at the END of the whole work package
> (session/stream boundary), not per slice.

Tracked `AGENTS.md`, § Gotchas:

> Append `# secret-scan: allow-this-line` to a false positive; do not bypass the
> hook with `--no-verify`.

**Why it breaks:** the public covenant forbids what the private grant makes the
default. Today that is invisible because the same person holds both. With
multiple developers it becomes a live contradiction: a contributor who reads
`AGENTS.md` and refuses `--no-verify` will be blocked by a pre-commit chain the
maintainer's agents routinely skip, and a contributor who inherits the habit from
an agent will be violating the covenant they were pointed at. Note also that the
grant's stated justification is a *pre-existing red gate* ("one hook [...]
currently CRASHES"), i.e. it is a workaround for gate debt, not a policy.

**Recommendation — MUST CHANGE.** Resolve it one way and write the resolution in
`AGENTS.md`. The defensible version is narrow: `--no-verify` is permitted only
for the specific hook that is known-red, with the end-of-package reconciliation
(the memory's own safety net: ruff + format, mypy, the full suite, a
`config/cicd/` displacement check) made an explicit, named obligation. A blanket
default that is only safe because one person always remembers to reconcile is not
a rule a team can inherit.

### A5. `--no-ff` merge policy

**Source:** memory `feedback_prefer_no_ff_merges`.

> When merging a feature or epic branch into a long-lived branch [...] use
> `git merge --no-ff` by default — even when the target hasn't moved and a
> fast-forward would be possible. [...] A `--no-ff` merge preserves the epic's
> commit sub-graph as a connected branch visible in `git log --first-parent`.

**Why it breaks:** a history-shape convention that only one person knows produces
inconsistent history the moment a second person merges. Under GitHub this is not
a convention at all — it is a repository setting.

**Recommendation — SHOULD CHANGE.** Set the repository's allowed merge methods to
match the intent (enable merge commits, and decide deliberately whether squash is
allowed), and state the reasoning in one line in `CONTRIBUTING.md`. The setting
enforces it; the line explains it.

### A6. The destructive-action boundary for git

**Source:** memory `feedback_bare_push_not_destructive`.

> **`git commit` is not destructive and never needs a gate.** [...] A bare
> `git push` is likewise not a destructive action and does not need an operator
> gate. Only flag-bearing pushes are destructive: `--force`,
> `--force-with-lease`, `--force-if-includes`, `--delete`, `--mirror`,
> `--prune`, or a push that rewrites refs.

**Why it breaks:** this is a genuinely useful, well-reasoned boundary that
prevents agents parking work unnecessarily — and it exists nowhere a contributor
or a contributor's agent can read it. Different people's agents will draw the
line in different places.

**Recommendation — SHOULD CHANGE.** This paragraph is nearly publishable as-is.
Put it in `AGENTS.md` § Commit Hygiene as the definition of which git operations
need a human decision. It is short, it is correct, and it is exactly the kind of
rule a new contributor's agent needs on day one.

### A7. Signing cadence and how loudly to report gate debt

**Source:** memory `feedback_gate_debt_is_operator_friction_point`.

> "we sign it once when the release branch merges, there's too much churn
> otherwise". So: don't list signed-entry drift as a remaining blocker or
> follow-up in release-branch reports, don't stage re-sign bundles mid-release,
> and don't count the signature CI lane as a gate for release-branch work.

> the operator is aware of the gate debt and holds the HMAC key himself; the
> red-gate state on merge is a **deliberate friction point** he uses to stay
> attentive to what's being landed.

**Why it breaks:** `AGENTS.md` does say the operator signs once at package
completion and that the red state is deliberate. What it does *not* say is the
reporting convention — that drift is expected noise mid-release and must not be
escalated. Without that, every new developer and every new agent will file the
red gate as a blocker, repeatedly. Separately, "a deliberate friction point *he*
uses to stay attentive" is a control that works because one person reviews
everything that lands; it does not survive a team.

**Recommendation — SHOULD CHANGE.** Add the reporting convention to
`docs/judge-signature-handoff.md` (one paragraph: what drift means mid-release,
when it is escalated, who escalates it). Then re-examine whether a red CI gate is
still the right attentiveness mechanism when landings are reviewed by PR — see
B2.

### A8. Agent tooling gets no tests and no docs in the public tree

**Source:** memory
`feedback_agent_tooling_is_not_tested_or_documented_in_the_public_repo`.

> "you can't just put the unit tests for this in tests/unit - you get that what
> you're making is not part of the project right? its tooling", then "it doesn't
> go in docs/, it doesn't have dedicated unit tests, its internal tooling", then
> "remove the test manager tests entirely, we don't test our own tooling in the
> public repo".

> **How to apply:** for anything under `.agents/skills/`, `scripts/` helpers or
> `.claude/`: no suite under `tests/`, no page under `docs/`.

**Why it breaks:** this is a real, enforced contribution rule with a concrete
trap. `.agents/skills/lane-manager/lane_manager.py` and
`.agents/skills/orchestrator/orchestrator.py` are tracked, non-trivial Python
with no tests in the tree. A conscientious new contributor will read that as a
coverage gap and add a test suite — which is exactly the mistake the rule exists
to prevent, and which would push tooling tests into the ~20-minute pre-merge gate
and the whole-tree AST gates' scope.

**Recommendation — MUST CHANGE.** One paragraph in `CONTRIBUTING.md`: which paths
are tooling rather than product, that tooling is deliberately untested and
undocumented in the tracked tree, and where its documentation actually lives
(beside the skill). Note the mild tension with this report itself — a review of
tooling landing under `docs/reviews/` — which I read as permitted, since the rule
targets tooling *reference documentation*, not a one-off review; flag it if that
reading is wrong.

### A9. A rule that fell out of the repository and now survives only in memory

**Source:** memory `feedback_filigree_cli_in_worktrees`.

> **Documented in [the repository's] `CLAUDE.md`** under the post-managed-block
> section "Filigree from a worktree" (the upstream `<!-- filigree:instructions -->`
> block is regenerated by filigree, so project addenda must live outside it).

**Measured today:** `CLAUDE.md` is 23 lines and contains no such section; its
only occurrence of "worktree" is the line pointing at
`scripts/worktree-cleanup.sh`.

**Why it breaks:** this is the cleanest exemplar of the failure mode this audit
is about. The rule *was* written in the repository, the repository was
restructured, the rule was dropped, and the memory store kept it alive — so it
still governs behaviour while being unreadable by anyone else, and its own
pointer now lies. Every other memory of this shape is invisible until someone
checks the citation.

**Recommendation — MUST CHANGE (and generalise).** Two actions. First, the
specific rule: whatever replaces it under GitHub Issues, put the
tracker-from-a-worktree guidance back into a tracked file. Second, the general
one: any memory that claims "documented in `<tracked file>`" is a claim to
verify, not a fact. Before the migration, sweep the memory store for that phrase
and re-verify each pointer.

### A10. Which agent plugin packs are enabled, and why

**Source:** memory `feedback_plugin_scope_core_skills_global`.

> **That is wrong on tokens.** Every enabled pack injects its agent / command /
> skill frontmatter `description` text into context on *every turn* of *every
> session and every lane agent*. Measured across [this repo's] 19 enabled packs:
> ~12,290 tok/turn total.

> **Deferred, [the maintainer] thinking about it (do NOT action unprompted)** [...]

**Why it breaks:** the tracked `.claude/settings.json` records the *result* (a
list of enabled/disabled plugins) but not the *reasoning*, the measurement
method, or the standing "do not action unprompted" list. A new developer looking
at that file sees arbitrary booleans and will flip them.

**Recommendation — SHOULD CHANGE.** A short `README.md` beside
`.claude/settings.json` (tooling documentation belongs beside the tool, per A8)
recording: why packs are disabled here, how the per-turn cost is measured, and
which decisions are explicitly parked.

### A11. Positive precedent — the one vocabulary that *is* written down

`docs/agents/tracker-label-vocabulary.md` exists and opens with exactly the right
justification:

> Written 2026-08-17 because the vocabulary had no definition anywhere in the
> tree, so every session re-derived it by sampling issues.

**Disposition — deliberate, keep.** This is the model the ten findings above
should be resolved into, and it must be migrated rather than abandoned (see D6).

---

## B. Single-holder assumptions

### B1. "There is only one agent running now" is the premise of a live safety rule

**Source:** memory
`feedback_commits_banned_until_signing_campaign_lands_2026-09-08`.

> **2026-09-09 LIFTED (standing, not one-off):** [the maintainer]: "ok, don't
> commit them yet (the judge is running) but lift the ban, there's only one agent
> running now". The ban's premise was concurrent agents racing the signed head;
> with a single agent it no longer applies.

> Expect the ban to be RE-ARMED whenever a head is being signed with more than one
> agent about; it has been re-armed twice already.

**Why it breaks:** the surviving mechanical rule is sound — "never commit while a
`sign-bundle` run is in flight, because the bundle binds an exact `source_rev`".
The *enforcement* is not: a conversational ban, re-armed by hand, whose lift was
justified by a condition ("only one agent running") that is false by definition
once the project has multiple developers. With N people, someone is always about
to commit.

**Recommendation — MUST CHANGE.** Replace the conversational ban with a
mechanical signing window. Options, cheapest first: the signing run refuses to
start unless the branch is protected against pushes for its duration; or the
bundle's `source_rev` check is made a required CI status so a racing commit fails
loudly rather than silently invalidating a staged bundle. Write the resulting
procedure in `docs/judge-signature-handoff.md`.

### B2. "The operator" is one unnamed person, and the key has no succession

**Source:** memory
`feedback_signing_is_one_operator_responsibility_outside_packages`; tracked
`AGENTS.md` [O1].

> "no individual packages of work are tracking the actual signature, I am
> tracking that as an operator responsibility, if every project has their own
> signing event nothing will ever close and therefore start signature."

**Why it breaks:** the custody rule itself is correct and should not change — an
agent must never hold the HMAC key, and signing must not run in CI. What breaks
is that "the operator" is a role with exactly one occupant, no named deputy, and
no written succession. If that person is unavailable, nothing can be signed and
therefore nothing can merge, because `CI Success` requires `static-analysis`.
That is a single point of failure for the whole project's ability to release.

**Recommendation — DELIBERATE, KEEP THE SEAM, BUT WRITE THE ROLE DOWN.** Do not
touch the two-actor seam. Do define, in `CONTRIBUTING.md` or the release
documentation: who holds the signing role, who the backup is, where the key is
escrowed, and what happens to a release if the holder is unavailable. Name it as
a role, not a person.

### B3. Shared-checkout and shared-index discipline

**Source:** memories `feedback_never_git_add_in_a_shared_checkout_commit_by_pathspec`
and `feedback_concurrent_writers_share_one_checkout_stop_me`.

> The commit (`2a3dc1100`) contained SIX files: mine plus [...] three test files
> belonging to a concurrent session that was mid-edit in the same checkout. Its
> work was attributed to my session and my commit message.
>
> **Why:** `git commit` with no pathspec commits the entire index, not the paths
> you just added. In a shared checkout the index is shared state.

> "if I ever do that again, you need to come here and stop me :p". Half-joking,
> real signal.

Tracked `AGENTS.md` carries the weaker half of this ("stage only your own
pathspecs"); the memory explicitly records that this is **not sufficient**:
"staging only yours does not stop someone else's staged files riding along."

**Why it breaks:** depends entirely on the discriminating question. On separate
workstations it is irrelevant. On a shared box it gets strictly worse: the
failure is silent, it mis-attributes authorship, and the recovery (`git reset
--mixed`, never `git restore`/`clean`) is itself a piece of unwritten expertise.
A human collaborator will not follow a pathspec-commit protocol nobody told them
about.

**Recommendation — MUST CHANGE if the box is shared; otherwise retire.** If
shared: one checkout per person, full stop — it is cheaper than the protocol. If
separate: delete the shared-index guidance from `AGENTS.md` rather than leaving a
rule whose rationale no longer exists, and keep it only inside the agent-harness
skills where concurrent lanes genuinely share a tree.

### B4. "At most one mutating agent per worktree"

**Source:** memory `feedback_a_reviewer_permitted_to_restore_is_not_read_only`.

> **A brief that says "you are read-only" and then says "restore anything you
> touch by cp round-trip" is NOT a read-only brief. The restore clause is a
> mutation licence.**

> Classify every reviewer as MUTATING or NON-MUTATING when you write the brief,
> not when you read the report. At most ONE mutating agent per worktree at a time.

**Why it breaks:** it does not break, exactly — it is agent-harness internals and
stays true regardless of headcount. What breaks is its home. The orchestrator
skill has written down the adjacent rule ("A lane you cannot prove is dead is a
lane you must not take", plus a real single-writer lock in
`.orchestrator/lanes.db`), but the *brief-classification* half — that a restore
clause silently converts a read-only reviewer into a writer — is memory-only,
and it is the half that caused a near-loss of an uncommitted fix.

**Recommendation — DELIBERATE, KEEP, BUT WRITE IT DOWN.** Move the
mutating/non-mutating brief classification into
`.agents/skills/lane-manager/SKILL.md` (which already covers brief construction)
or the orchestrator skill's non-negotiables. It is agent guidance, so it belongs
beside the skill, not in `docs/`.

### B5. A standing edit grant scoped to one named agent on one box

**Source:** memory `feedback_dev_server_full_edit_grant`.

> Operator (2026-07-02, verbatim intent): "[the agent] is authorised to edit the
> server install in any way it needs to, this is a dev server." Also explicitly
> authorized: deleting orphan tutorial data, wiping the sessions database, using
> the [provider] key in .env (cost-capped), and using [a shared demo account] on
> the demo server.

> **How to apply:** treat service restarts, config edits, DB wipes [...] and test-user
> creation on this box as pre-authorized.

**Why it breaks:** three ways. The grant names one agent identity, so it does not
transfer. It authorises destructive acts (wiping the sessions database) on a box
several people will now depend on for demos and live checks. And it rests on a
shared demo account and a shared provider key with a shared cost cap — under
multiple developers there is no way to attribute a spend, a wipe, or a session
to whoever caused it.

**Recommendation — MUST CHANGE.** Convert the personal grant into an environment
policy: state in a tracked deploy note what the demo box is for, that its data is
disposable, who may restart or wipe it, and that destructive acts are announced.
Replace the shared demo account with per-person accounts (the project already has
a user-management path for this), and give the provider key a per-developer
budget or move the cap to the account.

### B6. A global MCP trust widening accepted by one person

**Source:** memory `reference_mcp_servers_preapproved`.

> The **user-scope** `~/.claude/settings.json` `enableAll:true` covers those — but
> it **globally auto-trusts the `.mcp.json` of EVERY repo** opened in Claude Code
> (a clone with a hostile `.mcp.json` would auto-run). **Operator [name]
> explicitly authorized this global widening [...], informed of the blast
> radius** — do NOT tighten it back without asking.

> **Travel:** `.mcp.json` AND `.claude/` are gitignored → none of this travels via
> git; it's per-machine config.

**Why it breaks:** a documented risk acceptance, made by one person for their own
machine, with the explicit instruction not to reverse it. It cannot be inherited
— a new developer has neither made that judgement nor been told it exists, and
the instruction "do NOT tighten it back without asking" would, read by a new
contributor's agent, propagate a security posture nobody chose.

**Recommendation — DELIBERATE PER-MACHINE, BUT WRITE THE BOUNDARY DOWN.** Add a
line to `CONTRIBUTING.md`: `.mcp.json` is deliberately untracked;
`.mcp.json.example` is the template; each contributor decides their own MCP trust
posture, and the maintainer's setting is personal and non-normative. Verified
today: `.mcp.json.example` is clean — relative paths, no accounts, no
credentials — so it is safe as the published template.

---

## C. Machine-local coupling

### C1. A tracked settings file whose hooks require the maintainer's tools

**Source:** `.claude/settings.json` — **tracked** (`git ls-files` confirms).

```json
"SessionStart": [
  { "hooks": [ { "type": "command", "command": "filigree session-context", "timeout": 5 },
               { "type": "command", "command": "filigree ensure-dashboard", "timeout": 5 } ] },
  { "hooks": [ { "type": "command", "command": "loomweave hook session-start --path \"${CLAUDE_PROJECT_DIR}\"" } ] }
]
```

**Why it breaks:** every contributor who clones the repository and opens it in
this harness runs these hooks. Neither binary is a project dependency; neither is
installed by any documented setup step; `docs/maintainer/toolchain.md` states
plainly that none of it "is needed to build, test, or contribute to ELSPETH". The
`loomweave` hook carries no timeout at all. This is the maintainer's toolchain
wired into the shared configuration — precisely what ADR-043 split apart, leaking
back in through `.claude/settings.json`.

**Recommendation — MUST CHANGE.** Either guard each hook (`command -v filigree
>/dev/null 2>&1 && filigree session-context`), or move the tool hooks into the
untracked `.claude/settings.local.json` and leave only genuinely project-wide
hooks in the tracked file. Guarding is the smaller change and keeps the maintainer's
ergonomics intact.

### C2. A tracked guard that constrains every contributor, and a tracked file that describes it wrongly

**Source:** `.claude/settings.json` PreToolUse matcher `Bash`:

> `BLOCKED: dangerous git/proc command (commit --amend, pkill -f, checkout --) — refused by the PreToolUse guard in .claude/settings.json`

**Source:** `.claude/agents/red-team.md`, in the attack catalog:

> Run the test against the pre-change code (`git stash` is blocked here — use
> `git show <sha>^:<path>` into a temp copy, or read both versions)

**Why it breaks:** two separate problems. First, the guard blocks `git commit
--amend` and `git checkout -- <file>` for every contributor using this harness.
Those are reasonable protections *in a shared checkout* (the rationale is B3);
for a developer on their own machine they are friction with no stated reason, and
nothing in the repository explains why they exist. Second, `red-team.md` tells
every red-team agent that `git stash` is blocked — the guard does not block
`git stash`, and `AGENTS.md` confirms the stash prohibition is a convention with
no enforcement ("A `.git/hooks/pre-stash` once claimed to, but git has no such
hook"). A tracked file is making a false claim about a mechanism.

**Recommendation — MUST CHANGE (the false claim); SHOULD CHANGE (the guard).**
Correct `red-team.md` to say `git stash` is prohibited by convention, not blocked.
For the guard: keep it if the box is shared, but add a one-line comment naming the
rationale so a contributor knows why; drop or relocate it to local settings if
developers work on their own machines.

### C3. Machine-local absolute paths in an allow-rule

**Source:** `.claude/settings.local.json` (untracked, correctly gitignored).

Its single Bash allow-rule embeds a UID-scoped temporary directory and a
session-specific scratchpad path. Nothing leaks — the file is ignored — but it
shows permission rules being written with absolute, single-machine, single-session
paths, which means they silently stop matching and must be re-derived by whoever
inherits the workflow.

**Recommendation — SHOULD CHANGE (minor).** When writing allow-rules, prefer
patterns over absolute session paths. Worth a line in the `.claude/` README
proposed in A10.

### C4. A tracked document is wrong about where the system of record lives

**Source:** `docs/maintainer/toolchain.md`, inside the installer-written block:

> `filigree` tracks tasks for this project. Data lives in `.filigree/`.

**Measured today:** `.filigree` does not exist. The live database is under
`.weft/filigree/` — a directory the repository's own `.gitignore` labels:

> `# Retired tooling residue (ADR-043).`
> `.weft/`

The memory store already recorded this (`reference_filigree_mcp_vs_cli_actor`:
"the live DB is `.weft/filigree/filigree.db` (the installer-written toolchain.md
block saying `.filigree/` is wrong)").

**Why it breaks:** the project's entire work history sits in a path the
repository describes as residue of a retired tool, and the only tracked document
naming the tracker's location names the wrong one. Anyone migrating the tracker
by following the documentation will migrate an empty directory. The correction
exists only in personal memory.

**Recommendation — MUST CHANGE, before the migration.** Correct the path (it is
inside an installer-rewritten block, so the correction belongs in the
project-owned prose beneath it, or the installer's marker must be updated).
Confirm the `.weft/` gitignore comment is not hiding the live database from
backup tooling that trusts that label.

### C5. A push protocol that points at a script that does not exist

**Source:** memory (secondary index, "Moved from MEMORY.md 2026-09-07"):

> **★★★ PUSH ONLY VIA tools/push-slot.sh with a hand-written VERDICT — a chained
> line pushed a failed re-run**

**Measured today:** `tools/push-slot.sh` does not exist; `tools/` contains only
`pdf/`.

**Why it breaks:** a rule marked as top-priority in the maintainer's own index,
governing pushes, which cannot be followed. An agent reading that memory either
fails looking for the script or silently falls back to a bare push. The *reason*
behind it (a chained command pushed a re-run that had failed) is a real hazard
that the tracked `scripts/branch-safety-check.sh` now partly covers.

**Recommendation — SHOULD CHANGE.** Delete or rewrite the memory against current
reality, and confirm `scripts/branch-safety-check.sh` covers the hazard it
described. This is a second instance of the A9 pattern — verify every memory that
cites a path.

### C6. Deployment operating knowledge bound to one host

**Source:** memory `reference_elspeth_web_restart_and_session_db_traps`. Two
representative, host-neutral extracts:

> **A RESTART DOES NOT DEPLOY FRONTEND CHANGES** (measured 2026-09-04). The
> service serves a PREBUILT [frontend dist], so [a service restart] reloads
> Python and leaves the bundle exactly as it was. Merge a `.tsx` change, restart,
> and the browser still runs the previous build — a live trial then "passes"
> without ever executing the frontend code under test.

> **The SERVED config is [the deploy env file], NOT the project `.env`**
> (measured 2026-09-12). [...] the project `.env` is read by nothing in the unit
> and the two drift freely.

The same memory also carries the exact sudoers form, the socket path, the systemd
unit name, and which `systemctl` verbs are on the allowlist — all specific to one
machine.

**Why it breaks:** the host-specific parts are legitimately personal. The two
extracts above are not: they are properties of how the application is deployed,
and both describe silent false-pass modes. A new developer doing a live check
after a frontend change will get a clean-looking pass that never executed their
code.

**Recommendation — SHOULD CHANGE.** Split it. The deploy-shape facts (build the
frontend before restarting; verify the served bundle hash; the served env file is
not the project `.env`) belong in a runbook under `deploy/`. The sudoers form and
unit names stay personal.

### C7. Push access depends on a second account under one install

**Source:** memory `reference_gh_auth_for_pushes` (described generically:
the repository's remote is owned by a second GitHub account that the maintainer
operates alongside their default; pushes from the default account fail, and the
documented procedure is to switch accounts, push, and switch back).

**Why it breaks:** under multiple developers this dissolves into ordinary access
management — each person pushes as themselves — but nothing in the repository
says who grants push access, or that the repository owner is not the account most
contributors would expect from the commit authorship.

**Recommendation — SHOULD CHANGE.** One line in `CONTRIBUTING.md` on how to
request push access and who administers the repository. The account-switching
procedure stays personal.

### C8. Worktree layout stated as project fact

**Source:** tracked `AGENTS.md`:

> Worktrees live under `.claude/worktrees/<name>` and symlink `.venv` to the main
> checkout.

**Why it breaks:** mildly. The split-root `PYTHONPATH` discipline that follows it
is a genuine, well-documented project hazard and must stay. But the *location* is
one harness's convention — `.claude/worktrees/` is created by this specific
harness, and the memory store also references `.worktrees/` from an earlier era.
A contributor using plain `git worktree add` elsewhere gets none of the stated
setup and all of the hazard.

**Recommendation — DELIBERATE, KEEP, BUT SEPARATE THE TWO CLAIMS.** Reword so the
hazard (a worktree's bare `python`/`pytest` silently imports the main checkout's
editable install; both source roots must be on the path) is stated independently
of where the maintainer's harness happens to put worktrees.

### C9. The skill tree is published through tracked symlinks

**Measured:** thirteen of the fifteen entries under `.claude/skills/` are tracked
git symlinks (mode `120000`) pointing at `../../.agents/skills/<name>`; only
`explore-and-pin` and `loomweave-workflow` are real directories, the latter
because its `SKILL.md` is installer-generated and gitignored.

```
120000 004a1fa63c5abd186155ac2c8dfaacf3be6491e8 0	.claude/skills/bug-sweep
120000 5ce410e46a2715c6cec16870e87a58796f335c83 0	.claude/skills/orchestrator
```

**Why it breaks:** the design is good — one source of truth in `.agents/`, a
harness-specific view in `.claude/`, and no drift possible between them. The
portability is not. Git symlinks check out as plain text files on Windows unless
`core.symlinks` is enabled and the user has developer mode or elevation, so a
Windows contributor gets fifteen one-line text files where the skills should be,
with no error. The maintainer's platform makes this invisible today.

**Recommendation — SHOULD CHANGE (or accept explicitly).** If contributors may
be on Windows, state the requirement (`git config core.symlinks true`) in
`CONTRIBUTING.md` alongside the setup steps. If the project is Linux/macOS only,
say so once in `CONTRIBUTING.md` — it is a cheap sentence that prevents a
confusing first-clone experience.

---

## D. Tracker coupling

### D1. 1,595 tracked files cite a tracker ticket id

**Measured** (`git grep -l -E "elspeth-[0-9a-f]{10}" | wc -l`), with the
instrument controlled against a known-negative pattern (0 matches) and verified
against known-positive hits in `AGENTS.md`:

| Top-level path | Files citing a ticket id |
|---|---|
| `tests/` | 688 |
| `src/` | 643 |
| `docs/` | 177 |
| `elspeth-lints/` | 30 |
| `config/` | 14 |
| `deploy/` | 12 |
| `scripts/` | 10 |
| other (`evals/`, `.github/`, `examples/`, `website/`, `.agents/`) | 21 |
| **Total** | **1,595** |

`AGENTS.md` itself cites four (for example: "a one-element `IN` CHECK that
PostgreSQL reflects as `=` (elspeth-d0e62aea41)"), and
`docs/agents/recent-code-hints.md` cites 123.

**Why it breaks:** after migration to GitHub Issues, every one of those
identifiers points at a database that is no longer the system of record. Most are
in test docstrings and source comments explaining *why* a guard exists — they are
load-bearing context, not decoration.

**Recommendation — MUST CHANGE (plan it into the migration).** Do not rewrite
1,595 files. Instead, have the migration emit a tracked mapping file (for example
`docs/tracker-id-map.md` or a CSV) from each legacy id to its GitHub issue
number, and add one line to `CONTRIBUTING.md` explaining that `elspeth-<hex>`
identifiers are legacy tracker ids resolvable through that map. That preserves
every citation at a cost of one file.

### D2. The covenant names the tracker as the system of record

**Source:** tracked `AGENTS.md` § Filigree Issue Tracker, reproduced into
`CLAUDE.md`:

> `filigree` tracks this project's work. Use it to find, claim, update and close
> issues: `filigree session-context` at session start, then
> `filigree start-next-work --assignee <name>`.
>
> Two rules `--help` will not tell you:
> 1. Claim atomically [...] 2. On `SCHEMA_MISMATCH` [...]

**Why it breaks:** the harness-neutral covenant — the document that explicitly
promises "none of it is required to contribute" about the maintainer's toolchain
— mandates a specific local tracker binary. Under GitHub Issues this block is
simply wrong, and it is the first thing every agent reads.

**Recommendation — MUST CHANGE.** Replace with a tracker-neutral statement
("work is tracked in GitHub Issues; see `CONTRIBUTING.md`"), and move any
surviving filigree guidance into `docs/maintainer/toolchain.md` where the rest of
the personal toolchain lives.

### D3. The claim protocol is agent-native and has no human analogue

**Source:** `.agents/skills/filigree-workflow/references/team-coordination.md`:

> When multiple agents call `filigree update <issue-id> --status=<wip>`
> simultaneously, both think they own the issue. Filigree 2.0 solves this with
> `start-work`, which atomically claims the issue *and* transitions it [...] in a
> single DB transaction with optimistic locking on the assignee.

**Why it breaks:** GitHub Issues has no atomic claim, no compare-and-swap on
assignee, and no lease. The protocol was designed for a race between agents that
do not talk to each other. Humans coordinate by talking; the failure mode
(two people picking up the same issue) is recoverable and visible.

**Recommendation — MUST CHANGE.** Here is what each primitive becomes:

| Filigree primitive | Under GitHub Issues |
|---|---|
| `start-work` / `work_start` (atomic claim + transition) | Assign yourself + move the project-board column. No atomicity, no CAS — a collision is a conversation, not an error. |
| `start-next-work` (highest-priority ready issue) | A saved filter or project-board view; the agent picks and assigns. |
| `is_ready` = open ∧ no blockers ∧ **assignee empty** | A "no assignee" filter. The subtlety that an assignee silently hides work (D4) disappears — GitHub shows assigned issues normally. |
| 48-hour claim leases, `stale-claims` / `work_stale_list` | **Nothing native.** Becomes a triage-meeting question ("what has been assigned and untouched for two weeks?"), optionally a stale-issue action. |
| `verifying` status + hard `fix_verification` gate | A label (`needs-verification`) plus the linked PR. The hard gate is lost — decide whether to re-add it as a required PR checkbox. |
| `deferred` prohibition (see below) | A label-vocabulary decision: define the allowed close reasons and enforce them by convention or by an issue-close template. |
| Bug field chain (`severity` → `root_cause` → `fix_verification`) | An issue template with those fields, validated by review rather than by the tracker. |
| `close --reason` | A closing comment. |
| `close_commit` | `Fixes #N` in the PR description — strictly better, since GitHub links them automatically. |
| `observation_create` (14-day expiring scratchpad) | **Nothing native.** Either drop the concept or use a `nice-to-have` label with periodic pruning. |
| `--actor <name>` attribution | The authenticated GitHub user. Agents acting on someone's behalf need a convention for saying so. |
| Blocked-by dependency edges | Task lists / linked issues, or a project-board field. Weaker; no `work_blocked` view. |

### D4. Status semantics that only one person knows

Three examples, each a rule with real consequences:

`project_filigree_no_deferred_status_2026-09-10`:

> **How to apply:** when closing, use `closed` (with evidence), `wont_fix` /
> `not_a_bug` (bugs), `cancelled` (milestones) or `skipped` (phases/steps),
> always with a reason.

`feedback_verifying_is_not_closed`:

> `verifying` in this project means "locally fixed; live acceptance remains".

`reference_filigree_ready_means_unassigned`:

> `ready=False` with an empty `blocked_by` list means **someone is assigned**, not
> that a dependency is open. [...] an issue created with `assignee=claude` by a
> review lane [...] the claim lease expired [...] but the assignee column is never
> cleared, so `filigree ready` / `work_start_next` skip it forever.

**Why it breaks:** these are the project's actual workflow semantics, and they
are recorded in one person's notes. The migration is the moment they must be
re-stated deliberately, because the new tracker will not carry them.

**Recommendation — MUST CHANGE.** Write the close-reason vocabulary and the
verification convention into `CONTRIBUTING.md` (or a label-vocabulary document —
see D6) as part of the migration, not after it.

### D5. Tracked tooling that writes to the tracker

**Measured:** tracked files referencing filigree include
`.claude/workflows/dir-bug-sweep.js`, `.agents/skills/bug-sweep/SKILL.md`,
`.agents/skills/cicd-allowlist-audit/SKILL.md`, `scripts/red_team/trigger.py`,
`scripts/cicd/generate_skill_inventory.py`, and the five
`.agents/skills/filigree-workflow/` reference files.

The `bug-sweep` skill's own description is explicit:

> have them lodge verified findings in Filigree under one sweep tag, then
> reconcile the tracker into a clean, deduplicated, severity-ranked report

**Why it breaks:** two of these (`scripts/red_team/trigger.py`,
`scripts/cicd/generate_skill_inventory.py`) are under `scripts/`, i.e. in the
second auditor's territory as well — flagging the overlap rather than assuming
it is covered. The skills stop working entirely when the tracker goes away; the
scripts may fail at runtime.

**Recommendation — SHOULD CHANGE.** Inventory these five-plus call sites as a
migration work item. The `bug-sweep` pattern (fan out, lodge findings under one
tag, reconcile) maps cleanly onto GitHub labels and is worth porting rather than
dropping.

### D6. The label vocabulary must be migrated, not abandoned

**Source:** `docs/agents/tracker-label-vocabulary.md`, which defines the closed
7-value `p1-class:*` vocabulary and the open `lane:*` workstream axis, and ends:

> Do not mint new values. If something genuinely fits none of the seven, raise it
> as a vocabulary decision rather than inventing an eighth.

It also records an unresolved adjudication:

> **Open adjudication:** either `release-assurance` legitimately spans release
> horizons, or the five state-engine steps want a distinct value. Unresolved.

**Why it breaks:** it does not break — this is the *one* piece of tracker
semantics that is properly written down. But it is expressed in filigree label
syntax and cites filigree CLI verbs for keeping it current, so it needs porting
rather than preserving verbatim. The open adjudication is a decision that will be
silently lost if the file is treated as legacy.

**Recommendation — SHOULD CHANGE.** Port to GitHub labels with the same names,
keep the file as the definition (moved out of `docs/agents/` if that directory is
retired), and resolve or explicitly re-park the open adjudication during the
migration.

### D7. A bulk-write workaround that routes through one person's terminal

**Source:** memory `reference_auto_mode_classifier_blocks_bulk_tracker_writes`.

> a single Bash call running a dry-run-by-default close driver with `--execute`
> (46 issues, ~121 `filigree update/close/add-comment` commands) was denied by the
> auto-mode classifier as **[External System Writes]**

> **How to apply:** for a bulk tracker write, build it as a dry-run-by-default
> script with a logged plan, show the dry run, then give [the maintainer] the
> exact `! python3 <path> --execute` line to run himself.

**Why it breaks:** the *pattern* is good (dry-run default, logged plan, resumable)
and is exactly the shape the repository's own canonical scripts use. The
*resolution* is not: every bulk tracker operation funnels through one named
person pasting a command. That is a bottleneck and, with several developers, an
unattributable one.

**Recommendation — SHOULD CHANGE.** Keep the dry-run-default driver pattern —
write it down, since it generalises. Under GitHub, bulk operations go through the
`gh` CLI or the API under the acting developer's own credentials, which restores
attribution and removes the bottleneck.

---

## E. Agent process versus human collaborators

A note on scope first, to avoid inflating the count: a large amount of the memory
store is **harness ergonomics**, not project doctrine — addressing a teammate as
`team-lead` rather than `main`; putting a CWD-discipline header at the top of
every worktree brief; calling the advisor when a premise shifts; namespacing
scratch files per lane. A human contributor never needs any of it. I have
**excluded these from the findings** and mention them only so the absence is
deliberate. They belong exactly where they are (personal memory and the skills)
and should stay there.

What follows is the subset where agent process actually meets human
collaborators.

### E1. Lane/hub/seat vocabulary has no human referent

**Sources:** `.agents/skills/lane-manager/SKILL.md`, `.agents/skills/orchestrator/SKILL.md`,
and pervasively through the memory store ("the hub", "the writer seat", "lane
6e", "the metacontroller").

`lane-manager` is, to be clear, well-built and honest about what it is:

> **A lane's completion message is a claim, not a result.** The only things that
> count are on disk and in git [...]
>
> This file is the **harness-neutral procedure**. It assumes only that your
> harness gives you three primitives [...]

**Why it breaks:** as a *verification* discipline it survives contact with humans
intact and is worth keeping. As a *coordination* protocol it does not: a human
collaborator will not write `status.json`, will not heartbeat, and will not be
enumerated by `list-live()`. Any plan that treats developers as lanes will
produce lanes that never report.

**Recommendation — DELIBERATE, KEEP, BUT SCOPE IT EXPLICITLY.** Add a sentence to
`lane-manager/SKILL.md` Phase 0 stating that lanes are agent workers only, and
that human collaborators are coordinated through the issue tracker. The Phase 0
fit-check already models this kind of honesty about when the shape does not fit.

### E2. Coordination state is gitignored, so it is invisible to everyone else

**Source:** `.gitignore`:

> `# lane-manager per-lane state — working state, never public.`
> `.lanes/`
> `# orchestrator control plane: lane locks, step log, model observations.`
> `.orchestrator/`

and `.claude/lanes/` likewise. `AGENTS.md` § Subagent Reporting directs
coordination state there:

> Coordination and lane state go under `.claude/lanes/<run>/` — the
> `lane-manager` convention, already gitignored and already named in Commit
> Hygiene as never-stage.

**Why it breaks:** correct while one person runs every lane — the state is
working state and genuinely should not be public. With several developers, "what
is currently in flight" becomes a question only one machine can answer. The
memory store shows the cost already: dozens of entries exist purely to record
which lane landed what, because the lane state itself is unshareable.

**Recommendation — SHOULD CHANGE (the durable half only).** Keep `.lanes/` and
`.orchestrator/` gitignored — they are per-machine working state. Move the
*durable* facts they currently carry (what is being worked on, by whom, what
landed) into GitHub Issues, which is the shared surface the migration is
providing. `AGENTS.md` already distinguishes working state from durable
deliverables; this applies the same distinction to in-flight work.

### E3. The subagent reporting rule is sound and should be kept

**Source:** `AGENTS.md` § Subagent Reporting.

> A durable deliverable (a review, an inventory, a design note, an evidence
> bundle) goes to a tracked path under `docs/`. Confirm it with
> `git check-ignore -v <path>` before writing: no output means the path is safe.

**Disposition — DELIBERATE, KEEP.** This is the rule that makes reports like this
one survive. It works identically with any number of developers. (Verified for
this deliverable: `git check-ignore -v` on its path returned nothing.)

### E4. The fan-out capacity rule is one box's arithmetic

**Source:** memory `feedback_cap_test_parallelism_when_fanning_out_agents`.

> Measured 2026-08-17: ~12 concurrent cohort/lane agents each briefed
> `pytest -q -n 4` produced **load average 56 on 24 CPUs**, 455 Python processes
> and 107 node/chromium processes.

> Every agent starves every other, so wall-clock timings stop meaning anything —
> and agents then report **timing-induced flakes as defects**, which sends
> reviewers chasing regressions that do not exist.

and `docs/maintainer/toolchain.md`:

> a wide fan-out must brief an explicit per-agent test-parallelism ceiling,
> because 24 CPUs do not multiply.

`AGENTS.md` carries the human-facing version:

> Before starting a full suite on a shared host, establish who owns the test
> capacity, check for active suites and host load [...] Do not launch parallel
> full suites from separate agent sessions.

**Why it breaks:** "establish who owns the test capacity" is a social protocol
with one participant. With several developers it needs an answer, and the "24
CPUs" figure is one machine's. Note also the second-order effect the memory
records: capacity starvation manifests as *false defect reports*, so this is a
correctness problem, not just a speed one.

**Recommendation — SHOULD CHANGE, conditional on the shared-box question.** On
separate machines: reword `AGENTS.md` to drop "establish who owns the test
capacity" and keep the worker-count guidance as local advice. On a shared box:
it needs a real mechanism (a lock file, a queue, or CI running the full suite
instead of developers).

### E5. Model-selection and review-seat rules are personal but consequential

**Sources:** memory `feedback_review_work_never_goes_to_a_weaker_model`
("a model limit is not a reason to downgrade"), `feedback_a_casual_review_request_is_not_a_fanout_budget`,
and the two "first-class review seats" entries.

**Why it breaks:** these govern how much compute a review costs and who pays for
it. Under one maintainer that is a personal budget decision. Under several
developers — especially with the standing authorization in `docs/maintainer/toolchain.md`
telling agents to "spawn without asking, at whatever depth and fan-out the work
warrants" — it becomes a shared cost with no owner.

**Recommendation — DELIBERATE, KEEP AS PERSONAL, BUT BOUND THE GRANT.**
`docs/maintainer/toolchain.md` is already correctly framed ("This is the
maintainer's grant to their own agents"), so nothing there is wrong today. Before
new developers arrive, add one sentence making explicit that the standing
authorization is the maintainer's own and does not extend to contributors'
agents by default.

### E6. Fan-out concurrency hazards that are real regardless of headcount

**Source:** memory `reference_sibling_lanes_share_one_scratchpad_directory`.

> Every subagent lane fanned out from one session is handed the SAME scratchpad
> directory [...] Sibling lanes writing `before.txt`, `keys.json`, or any other
> generic filename overwrite each other with no error. [...] The corruption is
> silent and produces a *confidently wrong* answer.

> Note this memory directory is shared too: two lanes wrote THIS file
> concurrently and the later Write silently replaced the earlier one.

**Why it breaks:** it does not break with more developers — it stays exactly as
true. It belongs here only because the second paragraph shows the memory store
*itself* has a concurrency defect, which matters if the store is ever promoted
into shared documentation.

**Recommendation — DELIBERATE, KEEP.** Agent-harness internals; leave in the
skills. Worth remembering if any part of the memory store becomes a shared,
multi-writer document.

### E7. The tracker-from-a-worktree limitation becomes a non-issue

**Source:** memory `feedback_filigree_cli_in_worktrees`.

> filigree's CLI calls `realpath()` on the DB resolved from `.filigree.conf` and
> refuses to open it if the result lives outside the current project root. Inside
> a linked git worktree [...] the conf's relative `db` path resolves to a
> directory that doesn't exist

**Why it breaks:** favourably. This is a whole class of worktree friction — a
local-database tracker that cannot be reached from a linked worktree — that
disappears entirely under GitHub Issues, which is reachable from anywhere with a
network connection. Worth stating positively in the migration rationale.

**Recommendation — SHOULD CHANGE (delete the workaround).** Retire the
`(cd "$(git rev-parse --git-common-dir)/.." && …)` pattern and the shell-function
fallback from the skills at migration time rather than leaving dead guidance.

---

## Prioritised list

**Do before the first new developer has commit access:**

1. **A1** — decide and publish the multi-developer branching model. The current
   rule states its own premise as single-contributor and inverts under PRs.
2. **A4** — resolve the `--no-verify` contradiction between the private grant and
   `AGENTS.md`. A public covenant that forbids the private default will be
   violated by whichever side a new contributor reads.
3. **C1** — guard or relocate the tracked SessionStart hooks. Every clone
   currently runs two binaries no contributor is expected to have.
4. **B2** — name the signing role and its succession. Today, one person's
   unavailability blocks every merge, because `CI Success` requires
   `static-analysis`.
5. **A3** — publish the definition of done. It is the most-cited closure rule in
   the memory store and currently has no public home.

**Do as part of the tracker migration:**

6. **D1** — emit a tracked legacy-id → issue-number map. 1,595 tracked files cite
   ids that otherwise dangle.
7. **D2** — make `AGENTS.md`/`CLAUDE.md` tracker-neutral; move filigree guidance
   to the maintainer toolchain document.
8. **D3 / D4** — re-state the claim protocol and status semantics for GitHub
   (the mapping table in D3 is the starting point); publish the close-reason
   vocabulary.
9. **C4** — correct the tracker data-path claim *before* migrating, or the
   migration will follow the documentation to an empty directory.
10. **D5 / D6** — inventory the tracked tooling that writes to the tracker; port
    the label vocabulary rather than abandoning it.

**Do when the shared-workstation question is answered:**

11. **B3, C2, E4** — the shared-checkout doctrine, the tracked Bash guard, and the
    test-capacity protocol all resolve differently depending on the answer. On
    separate machines, delete most of it; on a shared box, mechanise it.

**Cheap corrections, do any time:**

12. **C2** — `red-team.md` claims `git stash` is blocked; it is not.
13. **C5** — a top-priority memory rule points at a script that does not exist.
14. **A9** — sweep the memory store for "documented in `<tracked file>`" claims
    and re-verify each; at least one is already false.
15. **C9** — state the symlink requirement (or the supported-platform decision)
    before a contributor clones on Windows and silently gets text files instead
    of skills.
16. **A6, A8, A10, B4, B5, B6, C3, C6, C7, C8, D7, E1, E2, E5** — write the rule
    down in the home named in each finding.

---

## Coverage of this audit

**Read in full:**

- `.claude/settings.json`, `.claude/settings.local.json`, `.gitignore`,
  `.mcp.json.example`, `.claude/commands/resume-lanes.md`,
  `docs/maintainer/toolchain.md`, `docs/agents/tracker-label-vocabulary.md`.
- `.claude/agents/red-team.md` (first 60 lines — the frontmatter and attack
  catalog; the remainder not read).
- Memory store, 27 files read in full: the branch/merge/push set
  (`work_on_release_branch_not_feature_branches`, `ready_for_merge_means_stop_before_landing`,
  `prefer_no_ff_merges`, `bare_push_not_destructive`, `no_verify_ok_with_end_of_slice_reconciliation`,
  `dod_is_merged_to_release_branch_and_green`, `never_git_add_in_a_shared_checkout_commit_by_pathspec`,
  `concurrent_writers_share_one_checkout_stop_me`); the signing/custody set
  (`commits_banned_until_signing_campaign_lands`, `signing_is_one_operator_responsibility_outside_packages`,
  `gate_debt_is_operator_friction_point`, `hmac_key_present_in_agent_shell_env`);
  the tracker set (`filigree_no_deferred_status`, `filigree_ready_means_unassigned`,
  `filigree_mcp_vs_cli_actor`, `filigree_cli_in_worktrees`, `verifying_is_not_closed`,
  `auto_mode_classifier_blocks_bulk_tracker_writes`); the machine/grant set
  (`elspeth_web_restart_and_session_db_traps`, `gh_auth_for_pushes`,
  `mcp_servers_preapproved`, `dev_server_full_edit_grant`, `agentic_tools_standing_grant`,
  `plugin_scope_core_skills_global`); and the fan-out set
  (`a_reviewer_permitted_to_restore_is_not_read_only`, `ping_a_lane_before_reclaiming_its_worktree`,
  `subagents_cant_use_worktrees`, `sibling_lanes_share_one_scratchpad_directory`,
  `cap_test_parallelism_when_fanning_out_agents`,
  `agent_tooling_is_not_tested_or_documented_in_the_public_repo`,
  `sendmessage_team_lead_not_main`).

**Read in part (headers, frontmatter, or a bounded sample):**

- `.agents/skills/lane-manager/SKILL.md` (first 70 lines),
  `.agents/skills/orchestrator/SKILL.md` (first 60),
  `.agents/skills/filigree-workflow/SKILL.md` (first 40),
  `.agents/skills/filigree-workflow/references/team-coordination.md` (first 60),
  `docs/agents/recent-code-hints.md` (first 40 lines of 2,359).

**Indexed but not read:** the memory store contains **773** files. I read 27 of
them in full — roughly 3.5% by file count, selected by following both index files
(`MEMORY.md` and `MEMORY-secondary-index.md`, both read in full) and prioritising
entries whose index line signalled a single-holder premise, a machine-local path,
a tracker convention, or an agent-process protocol. The indexes are themselves
curated by priority, so the sample is biased toward high-signal entries by
design — but it is a **sample**, and further single-holder assumptions almost
certainly exist in the 746 files I did not open. In particular I did not
systematically read the `project_*` lane-history entries, which are numerous and
where merge-protocol and seat conventions tend to be recorded incidentally.

**Not examined:** `.claude/lanes/` (63
directories) and `.claude/worktrees/` (32 directories) were listed but not
inspected. `.claude/red-team/review-log.md` and `.claude/workflows/dir-bug-sweep.js`
were identified as tracker-coupled but not read in full.
`docs/agents/sweeps/` was not examined.

**Measurements run, with their controls:**

- `git ls-files .claude .agents` — established which tooling files are tracked.
- `git check-ignore -v` on this deliverable's path — no output, path is safe.
- `git grep -l -E "elspeth-[0-9a-f]{10}" | wc -l` → 1,595. Controlled: the same
  expression with an impossible suffix returned 0, and the positive form returned
  real hits in `AGENTS.md` at named line numbers.
- `ls -d .filigree .weft/filigree` — `.filigree` absent, `.weft/filigree` present.
- `ls tools/push-slot.sh` — absent.
- `grep -n -i worktree CLAUDE.md` + `wc -l CLAUDE.md` — 23 lines, one worktree
  mention, no "Filigree from a worktree" section.

**Note on overlap:** `scripts/red_team/trigger.py` and
`scripts/cicd/generate_skill_inventory.py` (D5) sit in the second auditor's
territory as well as mine. I have flagged them rather than assumed coverage.

**Redaction control:** the finished document was grepped for the maintainer's
name, home paths, both GitHub account identifiers, the dev-server hostname, the
remote-environment id, the no-reply email and the temporary-directory prefix; the
same expression was run against a memory file known to contain several of them to
prove the instrument matches. The deliverable returns no hits.
