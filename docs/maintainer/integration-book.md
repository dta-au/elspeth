# How we run integration

This book describes how the maintainer's own agent sessions run integration.
Outside contributors need only [AGENTS.md](../../AGENTS.md) and
[CONTRIBUTING.md](../../CONTRIBUTING.md).

A new agent reads it before it touches a lane, a landing or the shared
machine. It collects the rules that govern integration work on ELSPETH and
states each one as it stands now.

Current as of 2026-11-23. Earlier rules that have been replaced are left out.
Where an agent might still follow an old rule, a one-line "this replaced X"
note says so.

## 1. How to use this book

**Read order for a new agent**

[M 2026-11-23] The maintainer asked for this book to be added as a session
start hook. `.claude/hooks/session-start-integration-book.sh` puts the short
version below into every session at start, resume, clear and compaction,
with this book's path. At start and after a clear it asks for the whole book
to be read before any work; on resume and after compaction it asks for the
sections the next step needs. Only the short version is injected because
Claude Code caps a hook's output at 10,000 characters. When the checkout has
no copy of the book, the hook copies it from a fixed trusted ref
(`origin/main`, then the current integration branch named in the script, as
last fetched) to `.claude/lanes/integration-book-copy/` and points there.
Update that ref when the integration branch changes.

1. The short version below (two minutes), then this book end to end (about
   35 minutes).
2. [AGENTS.md](../../AGENTS.md), the covenant for every agent. If this book
   and AGENTS.md disagree, stop and ask the integrator. There are no known
   disagreements (see below).
3. [CONTRIBUTING.md § Whole-tree gates](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit),
   before you write code.
4. The headers of the four canonical scripts:
   `scripts/worktree-cleanup.sh`, `scripts/branch-safety-check.sh`,
   `scripts/full-suite-gate.sh`, `scripts/examples-gate.sh`.
5. When you fan out lanes: the `lane-manager` skill
   (`.claude/skills/lane-manager/`) for verifying and merging lanes, and the
   `orchestrator` skill (`.claude/skills/orchestrator/`) for surviving quota
   limits, kills and duplicate instances.
6. The shared agent memory, starting at its index. Memory holds the live state
   this book leaves out.

**What lives here and what stays in memory**

| Here (stable rules) | In memory or private lane files (live or private) |
|---|---|
| How roles, rulings, scope, landing, tests, the shared machine, processes, models and public text work | Who holds each role today, by listed session name |
| Checklists | Current release state, branch tips, open queues |
| Rules, stated generically | Operational and environment facts |
| | Anything section 10 keeps out of tracked text |
| | Product rulings about composer behaviour |
| | Lock file locations, current box numbers if the box owner changes them |

**Disagreements with AGENTS.md**

None known. On 2026-11-23, at the maintainer's request, AGENTS.md was aligned
with this book on five topics: compatibility (section 4), temp files under
`~/.pt/<session>/<lane>` (section 7), scoped lane tests with one full gate on
the release branch (section 6), lanes not opening PRs (section 5), and staging
signature worklists only when the maintainer asks (section 10). If you find a
new disagreement, follow the stricter text and tell the integrator.

**Glossary**

- **Box:** the one shared machine (section 7). **Custody holder:** the
  integrator who sets the box's limits.
- **Lane:** one unit of work on its own branch and worktree, with a lane
  folder under `.claude/lanes/`. **Lead:** the session that owns a lane.
- **HANDOFF:** the file a lane gives the integrator to land it.
  **Handover:** the document one integrator or lead gives its successor.
- **Integration branch / candidate:** where the integrator stacks landed
  lanes for a release. **Release branch:** `release/**`, the branch a release
  PR comes from.
- **Red on base:** a new test fails on the base tree and passes on the lane.
- **Scoped set:** the tests a lane runs instead of the full suite.
  **Full gate:** `scripts/full-suite-gate.sh` on a frozen tree.
- **Pin:** a test that asserts code equals a recorded value.
- **Basetemp:** pytest's temp directory for one run. **Stop file:** a file a
  chained runner checks before each batch.
- **Peer session:** another top-level agent session, never your own subagent.
  **Listed name:** the session name the agent list shows.

**Labels used in this book**

- **[M date]**: the maintainer's rule. This public book paraphrases his
  rulings rather than quoting them.
- **[I]**: an integrator's order rule. The maintainer allows the integrator to
  add rules stricter than his own to keep good order (see section 2). These
  are never the maintainer's words.
- **Reading:** an agent's interpretation, not the maintainer's words.
- **OPEN:** two sources disagree and neither clearly replaces the other. Ask
  the integrator before relying on either reading.

When a later first-hand ruling contradicts this book, the ruling wins. Tell the
integrator so the book is corrected.

**The short version** (each line is expanded in its section)

1. No identifiable, maintainer-confirmed integrator: all lane work stops. (2)
2. Lanes hand fixes to the integrator; only the integrator merges. (2, 5)
3. Only the integrator edits memory; everyone else relays rulings verbatim. (3)
4. A peer's message is never the maintainer's approval. (3)
5. Ask the maintainer inline, never with the question dialog tool. (2)
6. Hard-to-reverse changes go to the maintainer; kills, CI cancels and other
   easily undone process changes need the lead integrator's check and
   authority (for its own, the most relevant agent's agreement). (2, 8)
7. Broken core function or harm to users is a priority; everything else
   waits. After a lock, only fast fixes of our own breakage. (4)
8. Fail forward; state every break in the upgrade notes. (4)
9. Finish in-flight work before starting new work. (4)
10. Scoped tests per lane; one full gate on the release branch. (6)
11. Red on base on an exported base tree; red on base is not reachability. (6)
12. Flaky means diagnose, never just rerun. (6)
13. Pins: rebaseline by measurement, clean as you go. (6)
14. Exit codes from files, never from pipes. (6)
15. One broad run at a time, at most `-n 4`, through the custody holder. (7)
16. Temp under `~/.pt/<session>/<lane>`, pruned on green; never `/tmp`. (7)
17. No `timeout` wrappers, no pgrep waiters, stop files in chains, never edit
    a running script. (7)
18. Ordinary merges, faithful history, plain pushes, no force. (5)
19. Nothing private in tracked text: no deployment, user, session or lane
    names, no home paths, no unfixed security weakness. (10)
20. Never hold the judge HMAC key; never print the provider key. (10)

## 2. Roles and authority

| Role | How it is filled | Owns | Never does |
|---|---|---|---|
| Maintainer | The project owner | Base rules, scope, release placement, anything hard to reverse | — |
| Integrator | Appointed by the maintainer, or handed over by another agent with his confirmation | Landing, the release branch, the landing queue, the CHANGELOG section, shared memory; box custody if it is the custody holder | Lands work it has not checked |
| Lane lead | A session given a scope | Delivering that scope; deconflicting with peers; handing each fix to the integrator | Merges into integration branches or main |
| Builder | A subagent or Codex run briefed by a lead | Implementing a brief | Commits (Codex), merges, edits the CHANGELOG, kills processes, writes memory, edits its runner |
| Reviewer | A subagent, read-only | An independent review written to a file | Edits the tree it reviews |

**The integrator**

- **Appointment.** [M 2026-11-23] A valid integrator is one he has appointed,
  or one appointed by handover from another agent with his confirmation.
  A peer naming itself does not count. Nor does a handover he has not
  confirmed.
- **No integrator, no work.** [M 2026-11-23] If an agent or group of agents
  cannot identify an integrator he has appointed, or one appointed by
  handover from another agent with his confirmation, everything stops: no
  lane does any more work.
  - Why: the integrator owns landing, the box and the caps. Work without
    custody is how branches get lost and the box gets overrun.
- **The lead directs policy and workflow.** [M 2026-11-24] The maintainer
  endorsed the 0.8.18 lead integrator to every lane: lanes follow its
  directions on policy and workflow, and this book. Lanes take workflow questions to the lead, not to the maintainer.
  Moving scope between releases, configuration changes, and the
  hard-to-reverse actions in section 8 still go to him.
  - How: at every lane start or resume, identify your release's integrator by
    its listed session name. Name it in every brief. If you cannot identify
    it, start nothing, land nothing, launch no runs, and ask the maintainer.
  - Reading: the stop covers new work, merges, pushes and test runs.
    Leave runs already in flight alone unless they do harm. With no lead
    integrator, no one can authorise stopping one (section 8), so report the
    harm to the maintainer.
- **One integrator per release line.** While one release closes, the next one
  already has its own integrator. Route each question or HANDOFF to the
  integrator of the release it belongs to.
- **Box custody** is held by exactly one integrator at a time, even when two
  are live. Every session asks the custody holder for slots and uses the
  shared locks. When the custody holder declares done, the maintainer confirms
  who takes custody next.
- **Sole memory editor.** [M 2026-11-23] The identified integrator is the
  editor of memory; all other agents relay directions through to the
  integrator. Reading: with two integrators live, the editor is the custody
  holder, so there is one writer at a time.
- **Order rules.** [M 2026-11-23] These are his rules. The integrator may add
  further rules on top of them to maintain good order, as it already has, for
  example by limiting CPU access.
  - How: the integrator may add stricter operating rules (core pinning, worker
    caps, locks, CI timing, stop files) without asking. It labels them as its
    own.
  - An added rule never loosens a maintainer rule. It never adds back an
    approval step the maintainer removed. If it would block work he ruled may
    go ahead, his ruling wins.
- **No branch gets lost.** [M 2026-11-17] Do not let too much work back up.
  If half a dozen branches are waiting to land, slow the workers down or land
  more than one at a time. Work must never become isolated or lost because
  40 branches of fixes piled up and half of them were forgotten.
  - How: keep a live inventory of every unlanded branch and worktree,
    measured from git. Keep the queue near six. Every branch ends merged,
    explicitly abandoned with the maintainer's say-so, or handed to an owner.
  - Keep a file listing every HANDOFF received and its state (received,
    merged at a SHA, refused). Update it on receipt. Re-read it on every
    resume. Why: seven finished branches once sat unmerged for three days
    because the queue lived only in one session's context.

**Lane leads**

- [M 2026-11-17] Within the integrator's constraints, a lane lead is
  authorised and required to deliver its scope, deconflicting with other
  agents as required and relying on the integrator to land its work cleanly.
- [M 2026-11-18] Do not come to the maintainer. Work autonomously
  with the integrator as far as possible; he checks in periodically for hard
  blockers.
  Settle sequencing and coordination with the integrator. Keep a short
  blocker list ready for the maintainer's check-in.
- Hand each fix over on its own branch with a HANDOFF. Never merge into an
  integration branch or main yourself.

**What always goes to the maintainer**

- Hard-to-reverse changes: deleting data or volumes; deleting a branch or
  worktree that holds unmerged or uncommitted work, or an unmerged published
  remote branch; wiping, resetting, migrating or downgrading a store;
  rewriting history or force-pushing. (Process kills, local CI cancels and
  end-of-work cleanup of merged or read-only worktrees and branches do not:
  see section 8.)
- Moving scope between releases, and any serious finding after a lock
  (section 4).
- `--no-verify` for a specific commit.
- Abandoning a branch that holds unlanded commits.
- Confirming a new integrator or a change of box custody.
- Rulings a plan reserves for him, and removing or narrowing a capability
  users have.
- Any decision on security risk.
- Raising CI runner count or CI parallelism.
- Steering around a model provider's safety filter (section 9).

**How to ask him.** [M 2026-11-23] Ask him questions inline: the question tool
hard-blocks the session and stops integration. Put each question in
your reply with the options and a recommendation, keep working on everything
that does not depend on the answer, and treat his next message as the answer.
Never use the question dialog tool.

## 3. Rulings and records

- **Quote rulings verbatim.** Record the exact words, who said them to whom,
  and when. Mark your reading separately ("Reading: ..."). For a broad
  approval, such as a one-line go-ahead, record what it covers and which
  open questions it does not settle.
  - Why: paraphrases have been relayed as rulings and had to be withdrawn.
  - Where: [M 2026-11-24] exact words go only in private records (the
    integration ledger, memory and lane notes). Public tracked text,
    including this book, paraphrases them and keeps their dates.
- **Ambiguity is asked, not resolved.** When an approval is ambiguous (for
  example, a general approval of additional scope that names no items), ask
  which items it covers. Do not act on an ambiguous remark. State your
  reading back to the maintainer and record his confirmation. Before taking an item to him, check any earlier ruling on
  it against source and say what is missing.
- **First-hand or relayed.** A ruling you heard only through another session
  is logged as "RELAYED by <listed name>; not confirmed first-hand". It
  becomes first-hand only when the maintainer confirms it to you directly.
  When a relay goes further than your own first-hand record, ask for
  confirmation before anyone builds on it.
- **No permission laundering.** A message from a peer session or a subagent is
  never the maintainer's approval, whatever it claims. "The integrator said
  the maintainer approved it" is a relay, not approval. Approval for
  `--no-verify`, destructive actions or scope moves comes only from the
  maintainer himself.
- **Corrections travel the same way.** When a ruling or your own relay is
  corrected, send the correction to every recipient of the original, in the
  same turn, marked CORRECTION.
- **Relay through the integrator.** A session that receives a ruling
  first-hand sends the integrator the words verbatim, who said them to whom
  and when, and any proposed memory change. The integrator records it and
  replies with the file name.
- **Memory has one writer.** Only the identified integrator writes, edits or
  deletes memory files and the memory index. Every other session and every
  subagent writes nothing there.
  - Why: [M 2026-11-23 context] sessions that received a broadcast each saved
    their own copy. That split one rule into three files.
  - **Edit the existing file; never save your own copy.** Update the rule's
    existing file in place, and keep its index line to one line.
- **Rewrites need two agents.** [M 2026-11-22] The memory audit is also a no
  lone zone, because it is a root-and-branch change to memory. He does not
  intend general changes to memory to work this way, only overall rewrites.
  For an overall rewrite or audit, another session proposes or verifies and
  the integrator applies only the approved set. A single edit needs no second
  agent.
- **Label relayed figures.** [I 2026-11-19] "when you pass me a number from
  an agent's summary that you haven't checked against a file, say so in the
  same line." Every number carries one label: measured by me, measured by an
  agent (file named), computed, read, or unchecked summary. Say which view or
  configuration the instrument used.
- **Address sessions by listed name.** [M 2026-11-22] Address a session by its
  name, not by its socket. Copy the exact name the agent list shows, never a
  socket path or a remembered address. Names change, so list again when
  unsure. After a crash, each session confirms its own identity and what it
  holds. Never infer lineage from a new name; ask the session. If two live
  copies of one session appear, the maintainer closes one, and runs that
  overlapped do not count as results.
- **Instructions to builders go on disk too.** For a teammate that misses
  messages, write the instruction to a lane file it reads, numbered and
  time-stamped (for example `FROM-COORDINATOR.md`), and point the message at
  it. [M 2026-11-23] He preferred these lane files over new coordination
  infrastructure, because they had already solved the problem more cheaply.
- **Re-verify status before you write it.** Before any status goes into a
  HANDOFF or handover, re-read the lane's latest report and logs, not a resume
  note. (AGENTS.md § Claims Must Be Measured.)

## 4. Releases and scope

**Release lines**

- A closing release and the next one run side by side. Each has its own
  integrator and branch. [M 2026-11-22] Work does not stop: the next release
  starts immediately, but a line does eventually have to be drawn.
- The closing release gets a closing list set in advance, with its decision
  rules fixed before the runs. Nothing else blocks it.
- The next release's branch advances to the released tip by fast-forward or
  merge, never by rebasing pushed commits.
- Deferred scope is kept in a tracked, pushed scope document for the next
  release (`docs/development/<version>-deferred-scope.md`). [M 2026-11-22]
  Make sure there is a durable list of all the scope that has been pushed;
  he does not want anything lost. Anything section 10 keeps out of tracked text
  is recorded privately instead.
- Only the maintainer moves items between releases; whether to pull scope
  forward is his call.
- **0.8.18 priority: the engine.** [M 2026-11-24] The engine is a key
  priority: prove its correctness first, then optimise it. Engine breaking
  changes are open but not guaranteed. Correctness work lands before
  optimisation, and each engine breaking change is stated in the upgrade
  notes; the bar below still protects what users can do today. The item list
  is section N of the 0.8.18 scope document.
- **Peelable fixes.** An urgent fix is cut from the latest release tag, not
  from the integration branch, and merged into the integration branch in
  urgency order. If a patch release is needed, the same branches merge into a
  release branch off main; nothing is cherry-picked. A fix that changes
  stored shape (epoch, CHECK, schema) lands on the integration branch only
  and cannot be peeled.
  - Why: the integration branch carries refactors and process changes a
    patch must not, so its prefix is not a shippable patch.

**The bar (standing for every release)**

- [M 2026-11-21, locked 2026-11-23] Anything that amounts to core
  functionality being broken goes to the current release; he defers minor
  and non-core fixes to the next. The bar is now set and locked: it does not
  mean backwards compatibility, but it does mean that whatever works now has
  to keep working.
  - How: broken core function, or anything harmful or embarrassing a user
    sees, is a priority for the current release. Minor and non-core fixes wait
    for the next one.
  - No accidental regressions. Before landing, ask: does anything a user can
    do today stop working or get worse? If so and it was not a deliberate
    design decision, it is a defect to fix before landing. [M 2026-11-23]
    The bar plainly does not extend to deliberate design decisions. A
    deliberate change is stated in the HANDOFF and the upgrade notes.
  - Test what the user sees (for example, that card text renders), not only
    that a code path runs.
- **If it's in, it has to work.** [M 2026-11-20] Anything shipped has to work.
  Do not scope a fix to the easy formats or modes and leave a shipped one
  broken.
- **Never tailor the build to one installation.** [M 2026-11-21] Do not
  shape the build around how any particular installation is configured.
  Judge a change on its general merit.

**Freeze and lock**

| | Freeze | Lock |
|---|---|---|
| Source | [M 2026-11-21] Scope is now frozen: nothing new is added unless a show-stopper is found | [M 2026-11-23] No further changes are considered after the lock time; anything new found later is a fast fix for something the release's own work broke, or it goes to the next release |
| New features | No | No |
| Necessary fixes, broken core function, harm to users | Yes: [M] his direction to freeze is not guidance that blocks necessary changes; the patch has to work first and foremost | No, unless it is a fast fix of breakage this release caused |
| Pre-existing defect found now | By severity | Next release, even if serious; raise a serious one with the maintainer rather than slip it in |

- **A fast hotfix** is a small, quick correction of breakage the release's own
  work caused. It is not a feature, and not a pre-existing defect found late.
- **The deferral bar.** [M 2026-11-22] Lean towards deferring, but reassess
  anything serious. Late findings default to the next release.
  Reading (the integrator's, not his words): serious means wrong results, an
  audit gap, data loss, or stuck or refused work. A serious finding goes to
  the maintainer before the merge. A fix may be built off the green
  candidate for evaluation; [M 2026-11-22] that is not yet a yes.
- **Bias to finishing.** [M 2026-11-21] Agents can still use their own
  judgement to surface scope or bring it in, but the general assumption is
  that the release should be put to bed. [M 2026-11-20] He would rather land
  what is needed first; if budget remains at the end, more can be considered.
- **Finish in flight before starting new.** [M 2026-11-15] The priority is
  completing work over failing fast; do not keep adding new work just because
  you are idle. Drive each in-flight lane to merged before you brief another.
  Why: wide work in progress delays every landing and once drove the shared
  machine into the kernel's out-of-memory killer.

**Fail forward and compatibility**

- **Fail forward.** [M 2026-11-22] The project fails forward. Do not revert or
  contort a fix to keep older in-flight runs or graphs working. State the
  break plainly in the upgrade notes: what refuses, and what the operator
  does.
- **Compatibility policy.** [M 2026-11-23] There is no backwards
  compatibility; that is the policy. There will be full backwards
  compatibility at 1.0. He may make a one-off variation for a specific need;
  that does not change the policy. Reading: before 1.0, write no migration
  code or compatibility shims for stores or records the current release
  refuses, and say in the release notes what stops carrying over. This does
  not cover a store the current release still opens: that store must read
  every shape its writers legitimately produced (see "Corrupt stored data"
  below).
- **Pipeline YAML.** [M 2026-11-15] YAML is the lowest-level extraction
  language, and the only one that must not break. [M 2026-11-16] A
  deliberate, documented plugin upgrade that refuses a YAML which ran with
  silently wrong behaviour is acceptable, with an upgrade note. Whether this
  line still stands as its own rule under the 2026-11-23 policy is not
  recorded. Either way, stop and raise any change that would alter or reject
  a valid pipeline YAML that loads and runs today.
- AGENTS.md § Project delivery posture states the same policy, aligned on
  2026-11-23 at the maintainer's request. What still holds:
  - no migrations, resets, wipes or downgrades of any store without his
    approval;
  - preserve/refuse contracts stay as they are;
  - every break is stated in the upgrade notes;
  - nothing a user can do today breaks by accident.
  Ask the integrator whether a change needs compatibility work.
- **Epoch and CHECK changes go ahead when required.** [M 2026-11-23] They
  can go ahead if they are required; regular review processes apply.
  From 0.8.18, a change to the session schema epoch or to a database CHECK
  constraint needs no separate approval when the work requires it. The
  hand-off names it, the reviewer checks it is needed, and the upgrade notes
  say that stores written by earlier releases are refused at startup and what
  an operator must do. Wiping, resetting, migrating or downgrading a store
  still needs the maintainer. Before 0.8.18 each such change needed his
  explicit approval.
- **Corrupt stored data fails closed.** [M 2026-11-22] Corrupted data is a
  bug, and the system should not proceed in a buggy state. In a store the
  current release opens, a shape an earlier writer legitimately produced is
  not corruption: it must still read.

**CHANGELOG**

- Builders never edit `CHANGELOG.md`. Every HANDOFF ends with a CHANGELOG
  entry text block.
- The release integrator writes the release's section, normally in one
  commit, from those blocks. It checks that every merge is covered.
- CHANGELOG text for security fixes follows section 10.

## 5. Landing

**From lane to integrator**

1. Ask the integrator for the base commit and a box slot. Cut one branch per
   fix, in its own worktree under `.claude/worktrees/`, from that base.
2. Build, test and commit. Commit validated work at once: an uncommitted tree
   once held four finished fixes for three hours.
3. Hand over with a `HANDOFF.md` (contents below). Never merge into the
   integration branch or main.
4. Prefer small, independent commits that each stand alone. Finished
   independent commits beat a large set.
5. When a lane is paused, stop its processes, commit its work locally as WIP
   on the lane branch (not pushed), record the SHAs and leave a RESUME file in
   the lane folder.

**What a HANDOFF carries**

- Head SHA, base SHA, files touched.
- Scoped tests with exit codes, and the selection file you actually ran
  (section 6).
- Red on base, measured on an exported base tree.
- Ruff over all of `src/elspeth` and mypy on changed files, with exits.
- The mock-discipline gate and every static step of the CI lint job, run
  verbatim from the current `.github/workflows/ci.yaml`, with exits.
- Whole-tree gates reached, or one line saying why none are reachable.
- The review file path, the diff hash before and after the review, and how
  each finding was settled.
- The keyless trust-tier finding set compared with the base (not with zero),
  and allowlist entries that need operator re-signing. Signature churn is an
  operator obligation; it never blocks a commit.
- Suite obligations still open: testcontainer, timing-sensitive, end-to-end,
  Playwright.
- The composer guidance diff (skill text, tool descriptions), or a statement
  that the change has no planner-facing effect (section 11).
- Size pins moved, with before and after. Pins touched, and whether each was
  kept, narrowed or deleted.
- "Smaller form chosen for speed": list any shortcut, do not rebuild it.
- Deliberate behaviour changes and their upgrade note; what stops carrying
  over.
- Whether the work spent provider budget.
- The CHANGELOG entry text block.

**Reviewing a HANDOFF (the integrator)**

- **"Cleared" means scope only.** [M 2026-11-15] He meant that the scope was
  cleared; the work should still be quality-checked. Do not dispute a
  cleared scope, but still check quality.
- Read the production diff in full.
- Check the red-on-base log fails at an assertion, not at import.
- Check the decisive claims from the evidence (logs, a failing assertion, a
  surviving mutant). Do not re-review everything.
- Check the scoped list was built on the current tip.
- Check commit messages against section 10 before merging. A pushed message
  cannot be fixed afterwards.
- Hold a change for an independent review when its risk calls for one. It
  lands only after that review clears and its findings are ruled.
- The reviewer works on a detached worktree at the handed-off SHA. The tree
  you merge must equal the tree that was reviewed.
- Your own reading of the code is not proof. Where a claim decides the
  landing, ask for an executed proof.
- "Accepted as code" is not "landed". A commit lands when a run on the merged
  tree covers it.
- Keep landing-order rules the lead states (for example, guidance text never
  lands before the code it describes).

**Merging into the integration branch**

- Run `scripts/branch-safety-check.sh` before every commit, rebase, merge and
  push, with the matching `--intent commit`, `--intent rebase`,
  `--intent merge` or `--intent push`, in the worktree you are acting in.
  Exit 1 means stop.
- `git merge --no-ff <sha>` with the message `merge <branch>: <summary>` and
  your session's trailers. Never rewrite history; merge forward.
- **Stack, then one gate.** [M 2026-11-19] Land all the code on the
  integration branch and land it from there; there are not the resources to
  run a full test suite for each individual fix. Unit tests and other
  relevant tests can still be run. Lanes land with scoped evidence. The
  integrator runs one wide run and one full gate on the stack.
- **When the tip goes red, nothing else lands** until it is green. [I] If the
  wide run on a merged tree fails, the lane is not landed: undo the local,
  unpushed merge (`git reset --keep`) and send the lane back for a fix round.
- Evidence from a throwaway merge counts only after you have checked its HEAD
  and tree are what you think they are.
- **Moving a shared tip.** [I] Before the integration tip moves, message every
  lead whose runs may read that worktree, name the commit, and wait until
  each confirms no run is reading it. A `/proc/<pid>/cwd` check is not
  enough: a harness can read a tree without its cwd there. Record whose
  evidence ran on which identical tree, and what was not run.
- **Keep the landing tree clean.** Peers and test agents never write into the
  integrator's landing worktree. End-to-end configs and specs live in the
  lane folder. Read-only checks on a peer's tree use
  `git --no-optional-locks status`.

**Rebase hygiene**

- [I 2026-11-18] Right after a rebase, run `ruff check src/elspeth` over the
  whole tree, and mypy the files both sides changed. Why: a textually clean
  rebase once dropped an import that newly landed code still used, failing
  eleven tests.
- Rebuild the scoped test list on the new tip and diff it against the old
  one. A count below the old base's count means the instrument is broken.
- Run the wide set after any rebase. Every recovered fix after one crash
  failed one to three newer pins on its first wide run.
- When a worker calls a failure "pre-existing on base", check which commit it
  used as the base.
- Use named two-dot ranges. Disable rerere for rebases
  (`git -c rerere.enabled=false`); it can replay another lane's resolution.
- After a crash, sweep every work branch with `git cherry <tip> <branch>`, and
  check behaviour, not commit subjects.

**Pushing**

- [M 2026-11-15] There is standing authority to push: get any and all ready
  code off the box onto GitHub. [M 2026-11-21] He meant only publishing
  the release branch itself; the work already done should not be lost.
- How: after each verified merge, push the candidate and release branches to
  origin. Plain push only: no force, no PR, not `main`, not another session's
  branch. Ask for anything else. This is stricter than AGENTS.md's leave to
  open draft PRs: lanes never open PRs, and the integrator opens release PRs.
- Run `scripts/branch-safety-check.sh --intent push` in the worktree being
  pushed. Never chain it to the push with `;`: a FAIL must stop the push.
- Branch names that match the CI push patterns (`main`, `master`, `RC*`,
  `release/**`) start CI. Others do not. A version cut may be held local when
  its name would start CI before you want it.

**Merging a release to main**

- `main` requires a PR. The ruleset's strict required checks are read from
  the **push-event CI run on the PR head**; Integration Tests runs only on
  push events.
- A PR from a branch outside the push patterns gets no push CI and cannot
  merge. Push the same SHA to a `release/**` branch as well.
- Opening a PR starts a second full CI run. Count it before telling peers CI
  has finished. A full CI run takes about 2.5 hours.
- Merging to main, and tagging, happen only when the maintainer authorises it
  for that release ([M 2026-11-22] for example, his authorisation to get a
  release onto main at that point). Before the merge: the fixes have landed,
  a leak review of the whole release delta and a README and CHANGELOG
  review have run and their fixes are applied, CI is green on the PR head,
  and the tutorial gate is clean.
- **Ordinary merge, faithful history.** [M 2026-11-22] Use a full merge: part
  of the project is a rapid prototype of AI-driven development, so history
  should be faithful where possible. No squash, no history rewrite to
  tidy messages. Auto-merge with the merge method works once checks pass.
- `gh pr edit` fails on PR bodies; use
  `gh api -X PATCH repos/<owner>/<repo>/pulls/<n>`.
- **Tags publish images.** A `v*` tag runs the image workflow as it exists at
  the tagged commit. That workflow refuses to publish unless main's required
  checks on the image SHA are green. So the tag goes on the merge that is the
  release, after main's post-merge CI is green and
  `scripts/cicd/check_release_required_checks.py` exits 0. Tags are annotated.
- The release CHANGELOG describes the behaviour of what shipped, not the
  intermediate defects fixed on the way, plus upgrade notes. Release-date
  edits are made at tag time.
- **Closing order.** [M 2026-11-22] Merge, then tag, then hygiene, then a full
  handover to the next integrator, then declare done to all peers.
  - Hygiene: [M] clean up worktrees and branches, make sure any Dependabot
    updates land, and do general hygiene. Use `scripts/worktree-cleanup.sh`.
    Land Dependabot through CI. Merged and read-only worktrees and branches
    go to the lead integrator for verification (section 8). List anything
    dirty, unmerged or holding unlanded work for the maintainer; do not
    remove it.

## 6. Testing

**Scope**

- **Scoped tests per lane; the full gate on the release branch.** [M
  2026-11-15] Before a merge, run only the subset of relevant tests; the full
  tests run on the release branch when a collection of work lands.
  [M 2026-11-22] Make sure lanes are not running the full test suite: they
  can run just the relevant tests, and then a full green run comes before
  the push.
- A scoped set is: tests for the touched modules, the new tests, ruff and
  mypy on changed files, and every whole-tree gate whose inputs the change
  could alter. State the selection and its exit codes in the HANDOFF.
- [Lead 2026-11-24] A branch that touches web worker or lifecycle code,
  `contracts/` or `core/` runs `test_web_landscape_mutation_fencing.py` and
  the fencing inventory before its HANDOFF. Those gates pin whole files by
  hash, so a locally green scoped run does not cover them.
- [Lead 2026-11-24] The integrator runs the whole `tests/unit/architecture`
  batch on the merged tree before every push of the integration branch, not
  only the gates the landed branches name.
- This does not relax AGENTS.md. A change whose effects scoped checks cannot
  bound (shared runtime behaviour, contracts, schemas, plugin registration,
  graph construction, test or scan infrastructure) still gets the full suite
  before it is merged for good. The lane says so in its HANDOFF and does not
  run the suite itself. The integrator schedules that run through the custody
  holder, normally as the stack's full gate on the local merge, before the
  stack is pushed. If it fails, the merge is undone (section 5).
- The full gate is `scripts/full-suite-gate.sh --execute --detach` on a frozen
  tree. Read `summary.txt`; `frozen=NO` means the run is not evidence. Note
  that the script defaults to 12 workers (8 with siblings) and a log
  directory under `/tmp`. Pass `--workers` and `--log-dir` to meet the box
  rules in section 7.
- The full-suite gate has no frontend stage. Before a release PR, run the
  frontend checks (unit tests, type check, lint, build) by hand on the final
  tip.
- A gate killed part-way through, or a preview gate, is not landing evidence.
- The pre-push set includes `tests/integration`, because CI's default
  selection does. When an integrator's "full run" lists its directories,
  check that it covers what CI's default selection covers.
- Do not re-run the full suite when the production tree is identical to the
  one already gated (AGENTS.md § Gotchas).

**Add these by name** (each was learned from a red that a green scoped run
missed)

| Change | Also run |
|---|---|
| A new composer tool, a changed tool result, or changed tool behaviour | All of `tests/unit/web`, `tests/unit/contracts` and `tests/integration/web` |
| Any composer lane | `tests/unit/scripts/test_composer_wire_census.py` and `test_composer_wire_scorecard.py`; `tests/unit/web/composer/test_error_code_redaction.py` |
| A new validation guidance code or error code | `tests/unit/web/composer/test_generation_response_contracts.py`, and update its wire baseline fixture |
| Skill text | `tests/unit/web/composer/test_capability_skill_identity.py` |
| A changed outgoing request shape that tests fake | Every fake, including those in `tests/helpers` and `tests/testcontainer`; run the testcontainer test that uses it |
| Web sessions, composer protocol or the session service | `test_web_reserve_reviewed_contracts.py` and `test_session_db_mutation_authority.py` on the rebased head |
| A plugin source file | Refresh `source_file_hash` with `scripts/cicd/plugin_hash.py`; run `tests/unit/elspeth_lints/test_plugin_contract_rules.py`, the DAG scenario-corpus contract test and its production-path test |
| Schema, SQL, session or Landscape persistence, or a lock | `pytest tests/ -m testcontainer -n 0` |
| Every lane | `tests/unit/test_mock_discipline_baseline.py` and every static step of the CI lint job, verbatim from the current `ci.yaml` |

- The full gate's lints stage is a non-blocking diagnostic. It does not gate
  the CI lint steps.
- A plugin edit also moves the web plugin policy hash, so runs admitted under
  the old hash refuse to resume. That is acceptable when stated, never as a
  surprise.
- Before a release gate, the testcontainer and timing-sensitive selections may
  be run early, serially, on an export of the candidate, when the remaining
  items touch no schema, SQL or persistence.

**Red on base, done right**

- Measure on an exported base tree:
  `git archive <base> | tar -x -C <lane dir>/base-tree`, copy in only the new
  or changed test files, and run the named test ids. Expect exit 1 with
  assertion failures. Exit 2 (failed to collect) is not red on base.
  - Why: reverting call sites while keeping new modules once made a test look
    red on base when it could not even import there.
  - Keep imports of new modules out of module scope in red/green test files.
- **Red on base is not reachability.** It proves the branch is wrong, not that
  a user can hit it. Before calling a finding critical or routing it to a
  release, prove reachability through the real entry point, or trace that the
  production caller can produce the triggering state. Until then, label it
  "reachability unproven".

**Flaky means diagnose**

- [M 2026-11-22] He wants a bit more effort put into flaky tests in this
  case, mindful that many of the flaky tests seen earlier were the real
  worker error that was later retired. Then: that one was a flaky test, but
  also a real system bug.
- How: never accept "passes at `-n 0`" or "passes on rerun" alone. Read the
  traceback, name the failure mode, and check it against known real defect
  classes (lease loss, worker death, eviction, deadline races, teardown leaks).
  Check the base commit, and whether the code under test changed. Report
  each flake as test-only (with evidence), production, or undetermined.
  Undetermined stays open.
- A known flake fix may wait for the next release only if the release CI
  passes. [M 2026-11-22] Push the fix to the next release as long as it
  can get through CI; otherwise fix it now.
- A fixed wall-clock deadline loses to a loaded runner. Make the deadline
  event-driven, not longer.
- A failure is environmental only when measured so (the same failure on an
  older head, or a pass once the environment is fixed).

**Pins**

- **Clean as you go.** [M 2026-11-22] This is not a matter of removing
  everything, unless that is the most sensible way to do it; it is cleaning
  as you go. When your work touches a pin, apply the keep test and say in
  the HANDOFF whether you kept, narrowed or deleted it.
- Keep a pin only if it guards a property with no behavioural form, a change
  needs a human to think, and its key holds only what matters (names or
  fingerprints, not line numbers; a ceiling, not exact bytes).
- Never put a pin in front of the behaviour check it protects.
- Do not unwind pins inside a closing release; an open lane re-pins as normal.
- **Rebaseline by measurement.** [M 2026-11-18] There are no hard caps: take
  the easy wins and rebaseline. A size or count pin a change legitimately
  moves is moved by the measured amount and reported. It is never a review
  finding or a blocker. Gates that check behaviour are unaffected.
- For composer instruction text: [M 2026-11-19] A test cap exists to stop the
  text bloating, but that does not mean nothing may ever be added. Add the
  guidance the model needs, keep it tight, move the pin, report it. If a
  builder trimmed guidance to fit a pin, the HANDOFF says so.

**Evidence discipline**

- Exit codes come from files: `pytest ... > "$log" 2>&1; echo "exit=$?"`.
  Never from `tail`, `grep` or a pipe (AGENTS.md § Test Verification Policy).
- Exit 5 (nothing collected) is a broken instrument: fix the paths and re-run.
  Never count it.
- In a worktree, put both source roots on the path and check that
  `elspeth.__file__` and `elspeth_lints.__file__` resolve inside it.
- Red results from a disk-full window are not results.
- Timing- and lease-related behaviour that passes on an idle machine is weak
  evidence. Test it pinned to few cores under load.
- For any per-turn or per-call path, assert the growth ratio at two sizes, not
  one size. Unbounded growth with session or graph size has been the
  recurring defect.

**Environment**

- [M 2026-11-21] Agents are authorised to install and configure as needed so
  that the tests reflect the correct environment. Install the way CI does (for
  example `npm ci` in the frontend directory, `npx playwright install
  chromium`) and say what you installed.
- This never covers `uv pip install` or `uv sync` in a worktree: that clobbers
  the shared venv. It is not approval to change code or pins. If the shared
  venv's installed metadata lags the release version, leave it; refreshing it
  is the maintainer's call.
- A browser test suite with no `node_modules` fails as file-not-found. That is
  environmental.

**Live provider runs**

- The tutorial is an acceptance gate for every skill-text or tool-surface
  change. Run it end to end on the candidate and on the base with the same
  model, and compare completion, turns, time, provider calls per transition
  and the final graph.
- Clear every live run with the integrator first. Acceptance runs have
  priority on the provider budget.
- State in every report whether provider calls went out. Probes that do not
  need a live model stop before the provider or use fakes.
- A lane-private local server with no prompt guard service configured needs
  `ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE=debug-when-unconfigured` in its own
  start script or process environment, never in a tracked file. Say in the
  run report which guard mode the server ran in.
- [M 2026-11-22] The maintainer runs live tests on DeepSeek partly because it
  is more chatty and its reasoning is visible. Quote a few of the model's own
  statements in run reports.

## 7. The box

"The box" is the one shared machine. Every agent session, every local test
run, the Codex sandboxes and the self-hosted CI runners share its CPU, memory
and disk. The custody-holding integrator sets its limits; the rules below are
the integrator's order rules [I] unless marked [M].

**Current limits** [I 2026-11-22]

- One broad test run at a time across all sessions, and only through the
  custody holder.
- At most `-n 4` workers per run.
- At least 12 GB of memory available (`free -g`, the "available" column)
  before a broad run starts. [M 2026-11-23] The figure means memory; the disk
  shortage that once also applied has been fixed. Still read `df -h /` before
  a broad run, and stop if the disk is nearly full.
- Short named-file runs (under 5 minutes, at most `-n 2`) take a short lock.
  Longer runs take the long lock, granted by the custody holder.
- New lanes get a short-lock-only limit, pinned to a core range with
  `taskset`, until the custody holder raises it.
- Static checks take no lock.

**Locks**

- Every session takes the shared lock files (`box-pytest.lock` for long runs,
  `box-short*.lock` for short ones). A per-session cap does not add up to a
  machine cap.
- Copy the lock path from the custody holder's current instruction. Why: a
  brief once used a different path for the same lock name, and runs went
  unserialised for a day.
- Measure the machine total with `ps` before granting slots.

**The coordinator owns the runner**

- Before dispatching a build agent, the lead writes the lane's `run.sh`
  itself. It hardcodes the lock, the `taskset` core range, the worker cap and
  the stop-file check. It refuses arguments that exceed the limit, including
  wide directories, non-test paths and no path at all (pytest then runs the
  whole suite). It is marked coordinator-owned; the agent may not edit it or
  run tests outside it. Test its refusals before the agent's first run.
- Why: a briefed builder still wrote its own runner and launched a 75-minute
  run on the long lock during a release gate. A limit in prose can be lost or
  reasoned away.
- When the box limit changes, change the runner, not only the brief.

**Temp and disk**

- [M 2026-11-19] Get rid of the tmpfs and just use the SSD. Every test
  or tool run, and every brief, exports `TMPDIR=~/.pt/<session>/<lane>`.
  Pytest `--basetemp` and exported base trees go there or in the lane folder.
  Nothing new goes under `/tmp`.
- `~/.pt/<session>/<lane>` is the one exception to AGENTS.md's "inside the
  project folder" rule, and it is for temp files only. It is on disk, outside
  `/tmp`. Project work (code, lane notes, reports, deliverables) stays in the
  repo or a worktree.
- Give every pytest invocation a unique temp root (add a timestamp). Why: a
  killed orphan's cleanup step once deleted a live run's database files from
  a shared temp root. `--basetemp` must be a fresh directory of its own:
  pytest once wiped a parent directory that held an exported tree. AF_UNIX
  socket tests need short roots (a 16-character root passes where a
  35-character one failed).
- A run deletes its own basetemp when it exits 0 and keeps it on failure. Keep
  logs. Each session prunes only its own `~/.pt/<session>/` folder. Why: unpruned
  basetemps filled the root disk to zero bytes and failed three sessions'
  runs.

**Runners and waits**

- **Stop files.** Every chained runner checks a stop file between batches
  (`[ -f "$LANE/STOP" ] && exit 0`) and names the stop path in its first
  report. When a hold arrives, read each running chain's script to confirm the
  check exists before telling the integrator the hold is honoured.
- **Never edit a running script.** Bash reads a script as it runs; an in-place
  edit makes the running copy execute shifted bytes. Write the new script to
  a temp file in the same directory and `mv` it over the old one. Check `ps`
  for its runs in the same command as the swap.
- **No `timeout` wrappers or watchdogs.** Self-stopping wrappers count as
  kills (section 8), and a stopped pytest can leave orphans. Put this line
  verbatim in every builder brief: "Never wrap a command in `timeout` or any
  self-terminating watchdog; if a run must stop, ask your session." Tests
  turn a hang into an in-process failure (a daemon helper thread with a
  bounded join, then assert).
- **No pgrep waiters.** `until ! pgrep -f X` matches its own command line and
  never exits. Wait on an `.exit` file the run writes, or on `$!` polled with
  `kill -0`. Never start a waiter with its cwd inside a worktree you will
  want to remove.
- **Poll the disk, not messages.** Task notifications get lost. While agents
  run, check every lane folder about every 30 minutes for new `.exit` files,
  write times and processes (`/proc/*/cwd`). Wake an idle agent with the
  measured state. Why: four agents once idled three hours on lost
  notifications with all work uncommitted.
- **Holds.** When the custody holder sets a hold, running rounds finish and
  nothing new launches until it clears. A hold set by the maintainer clears
  only on his word. Stop a queued waiter with a hold-file check in its script,
  not with a signal. The maintainer may also reserve the box for one release
  or one lane until it ships.
- A lane told to use no box resources runs no tests, measurements or builds;
  its claims stay "by reading". OPEN: an earlier allowance for such a lane to
  run ruff was withdrawn pending the maintainer's answer, and no answer is
  recorded. Until there is one, run nothing.

**The CI runners**

- The self-hosted CI runners run on the box and are limited by disk writes.
  [M] Do not re-enable extra runners or raise CI parallelism without asking
  the maintainer. Prefer more workers per job over more concurrent jobs.
- While the full gate or a release's CI runs, peers stay at `-n 2`, one run at
  a time, with no broad runs, no Playwright suites and no new Codex launches.
- [M, recorded for one release] When it is pushed, all local testing must
  stop until CI clears. Ask the custody holder whether this applies to
  the push you are making.
- The shared venv's editable install can be re-pointed silently (a Playwright
  web server's `uv run` in a worktree does it). Check that `elspeth.__file__`
  in the main venv resolves to the main checkout; restore it and tell peers.

## 8. Processes and CI

**The two-person rule**

- [M 2026-11-19] Agents must get independent verification from a peer before
  terminating processes.
- [M 2026-11-23] An agent that can convince a peer that a process-destructive
  change (one that is relatively easily reversed in any case) is valid, and
  gets authority to kill, is allowed to do so. This was always the rule, and
  it is not limited to tests.
- [M 2026-11-23] All process kills go through the lead integrator. So a kill
  needs a case made to the **lead integrator** (a session, never your own
  subagent) and its explicit authority; any other peer is not enough. Then no
  maintainer approval is needed. With no identifiable lead integrator,
  nothing is killed.
- **The checker.** In this section "the checker" means the lead integrator,
  or, for the lead integrator's own kills, cancels, deletions and closes, the
  most relevant agent available or a fresh independent reviewer (see "The
  lead integrator's own close-outs" below).
- **CI is a process.** [M 2026-11-23] He does not see how locally hosted CI
  could be anything other than part of that. A CI cancel on the self-hosted
  runners falls under this rule; do not ask the maintainer.
- **End-of-work cleanup.** [M 2026-11-23] The deletion rule (the no lone zone
  rule) extends to ordinary deletion of worktrees and branches at the end of
  a line of effort, when the work has merged into the release branch or was
  a read-only activity. [M 2026-11-23] The rule names the lead integrator
  explicitly as the peer, in line with the rule that no lead integrator
  means everything stops. So make the case to the **lead
  integrator** and get its explicit authority; any other peer is not enough.
  With no identifiable lead integrator, nothing is deleted.
- **Merged means durable anywhere.** [M 2026-11-23], asked whether this covers
  the integration branch: yes; branches that are no longer required, because
  the work is durable in another branch, need not be kept in duplicate or
  triplicate. Reading (the lead integrator's): durable means pushed to
  origin. A containing branch that exists only locally does not count.
- **The lead integrator's own close-outs.** [M 2026-11-23] When the lead
  integrator wants to close out a position (a process, a run or anything
  else), it gets agreement from the most relevant agent available, or starts
  a new one to review it independently. The intent is to stop accidental
  error; there is no presumption that the agent is hostile. So for its own
  kills, cancels, deletions and closes, the lead integrator gets agreement
  from the most relevant agent available, or from a fresh independent
  reviewer. Keep it proportionate: the check catches mistakes; it does not
  police.
- **What the lead integrator checks before a deletion.** Reading (the lead
  integrator's): it checks
  - merged: the tip is an ancestor of a pushed branch that holds the work
    (`git merge-base --is-ancestor <tip> origin/<branch>`, against the origin
    ref after a fetch); for a squash or rebuild, the content difference
    against that pushed branch is zero, or only what the landing integrator
    recorded;
  - read-only: no commits or unique content beyond its base;
  - every tree: clean `git status`, ignored content inspected and not needed,
    and not in use.
  Then use `scripts/worktree-cleanup.sh`, or a plain `git worktree remove` (no
  force), then delete the branch with `git branch -D` gated in the same
  command by a fresh fetch and `git merge-base --is-ancestor <tip>
  origin/<branch>`, and report each tip hash.
  - Why: `git branch -d` tests against the checkout's own HEAD or the
    branch's upstream, not the branch that holds the work, so it refuses a
    lane branch whose tip is in the integration branch.
- **Hard to reverse stays with the maintainer:** deleting data or volumes,
  deleting unmerged or dirty work, deleting an unmerged published remote
  branch, wiping shared state, and anything the lead integrator, or for its
  own close-outs the agent reviewing them, is unsure of.
- Why: a parent-PID walk once sent SIGTERM to every session on the box. Single
  sessions have also misattributed runs and mistaken their own `pgrep` shell
  for a new run. The safeguard is the second pair of eyes.

**How to kill a process**

1. Take a read-only snapshot: pid, ppid, age, cwd, command line, children,
   lock holders, and `/proc/<pid>/task/*/wchan`.
2. Send the checker the exact PIDs, each with its command line, cwd, parent
   and age, and why each is yours.
3. The checker checks with its own `ps` and `/proc/<pid>/cwd`, then confirms
   or objects, and grants authority.
4. Re-check each PID's identity immediately before signalling.
5. Signal only the confirmed PIDs. Use the gentlest signal that works
   (SIGINT lets pytest tear down), one signal, then re-check. Escalate to
   SIGTERM, then SIGKILL, only with the checker's concurrence. A pytest whose
   event loop absorbs SIGINT needs SIGTERM.
6. After-check: every confirmed PID is gone, its children exited with it,
   ports are free and the tree status is unchanged. Report what was stopped.
   A kill made without the checker's check is disclosed with exact PIDs, time
   and signal.

- Exact PIDs only. Never walk from a parent: an orphan's parent is PID 1.
  Never kill by pattern (`pkill -f`). Refuse any PID at or below 1 and any
  agent harness process.
- **The Bash guard.** [M 2026-11-23] Block those shapes as well, with a
  message saying to speak to the lead integrator for authorisation.
  `.claude/hooks/bash_guard.py`, run before every Bash tool command, refuses
  `pkill`, `killall`, `kill -1`, `kill 0`, `kill -- -PGID`, `kill $(...)`,
  `xargs kill` and `killpg`, including behind wrappers such as `sudo`,
  `timeout`, `bash -c` or `eval`, and points here. It also refuses
  `git commit --amend` and, [M 2026-11-23] blocked at his request because a
  few early losses happened that way, both `git checkout -- <path>` and
  `git checkout <ref> -- <path>`, which overwrite uncommitted work. It checks
  only what the shell would run: quoted text, heredoc bodies and comments
  are ignored, so a commit message or a grep that mentions these commands
  still runs. A kill of explicit numeric PIDs (`kill -TERM 12345`) still
  runs. The guard is a backstop for slips, not a lock: it does not see
  Codex, scripts that stop their own children, or commands typed by hand, so
  this procedure still applies to all of them. If the script is missing,
  commands run unguarded with a warning ([M 2026-11-23] a warning is
  acceptable). A refused kill is never retried in another form; take it to
  the checker. If the guard refuses something that is not a kill, tell the
  lead integrator so the guard can be fixed.
- For an orphan, stop its shell first, then pytest, so its cleanup step
  cannot delete a live run's files.
- A script that stops its own children counts: name the PIDs to the checker
  before the run, or have the script print them and wait.
- An order to another session to stop work includes the checker's check.
- **Subagents never kill.** They send the PID table to their lead, who relays
  it to the checker and returns the confirmation. Put this in every brief.
- Reading: stopping one of your own harness tasks through the harness is not
  a PID kill. If unsure, ask the checker anyway.

**CI cancels**

- Cancelling runs on superseded SHAs and Dependabot runs is routine, with the
  checker's authority.
- Keep both CI runs on the current PR head, and main's post-merge push CI.
  Cancelling the first blocks the merge; cancelling the second blocks the
  image publish. Both are undoable (`gh run rerun <id> --failed`), but a
  re-run costs up to about 2.5 hours. Cancel them only when the box must shed
  load, with the checker's authority, and re-run before merging or tagging.
  [I; on 2026-11-23 the maintainer replied to this alignment with the
  order-rules ruling in section 2]
  - This replaced "never cancel push CI on a PR head or main". The cost
    advice behind that rule still stands.
  - Why: a cancelled push run on a PR head once left the PR blocked with "5 of
    5 required status checks are cancelled" despite green PR CI. A cancelled
    post-merge run on main once made the image build fail.

## 9. Subagents and models

**The current model rule**

- [M 2026-11-21] The top-tier review model (named in memory) is for complex
  review only. Regular work (ordinary review, coordination) runs on Claude
  Opus. Building and running runs on Codex.
- **Design and wording.** [M 2026-11-23] Wording still counts as design and
  should be done by the design agent of the time. Which agent that is
  depends on available tokens: an external agent, Opus, or a higher-tier
  model. Composer instruction text (skill, tool descriptions, hints) is design
  work. When unsure which design agent is current, ask.
- New dispatches follow the rule. In-flight agents finish as they are. Do not
  interrupt a builder mid-change to switch model; its next task goes to a new
  agent briefed from the lane files.
- Forks inherit the parent's model, so do not fork for hands-and-feet work.
- The commit trailer names the model that actually did the work.

**Briefs**

Every brief names:

- the integrator, by listed name;
- the scope, files, and what not to change;
- acceptance: red on base, green on the lane, the named tests;
- where to write findings: a file, returning only the path and a short
  summary (AGENTS.md § Subagent Reporting). A durable deliverable goes to a
  tracked path under `docs/`, checked with `git check-ignore -v`;
- the allowed write locations, and that the tree must be clean at the end;
- the box rules: lock, worker cap, `TMPDIR` and basetemp under
  `~/.pt/<session>/<lane>`, prune on green, stop file, the coordinator-owned
  runner;
- the no-`timeout` line and the no-kill rule, verbatim;
- no memory writes, no CHANGELOG edits, no `--no-verify`, no suppressions;
- that any session schema epoch or CHECK constraint change is named in the
  hand-off and in the proposed upgrade notes;
- that every measurement names the view or configuration it used.

**Reviewers and claims**

- Brief a reviewer with the code, not with your conclusion. Review is
  independent only when the context is.
- A reviewer must not edit. Hash the diff before and after
  (`git diff HEAD | sha256sum`); a changed hash voids the review.
- Treat a subagent's decisive claims as claims. Verify them from the evidence
  before you hand off. Record each finding as CONFIRMED or REJECTED with
  evidence; only confirmed findings get a fix round.
- A "no finding" from a weak reviewer is not assurance on its own.
- Read-only subagents take no box slot. A subagent that runs pytest, a build
  or Docker counts as a lane on the box.

**Codex specifics** (live while Codex does hands and feet)

- Invocation: `codex exec -m <model> -c 'model_reasoning_effort="<level>"'
  -s workspace-write -C <worktree> --add-dir <lane dir> -o <file> - <
  prompt.md`. Add only the lane folder and `~/.pt/<session>/<lane>`; never
  `--add-dir $HOME`, which exposes agent settings and memory.
- Codex's lighter model implements, runs and measures. Its stronger model
  analyses and reviews at medium reasoning effort at least, high for
  important code (credentials, persistence, provider request building,
  audit). The model ids are in memory.
- Ask the custody holder before every Codex launch. A granted slot belongs to
  the lane through its whole build-review-fix loop.
- Codex runs pytest only on named test files, never a directory, and stops
  rather than start anything expected to take more than a few minutes. Broad
  gates run outside the sandbox.
- The sandbox has no network sockets. Socket tests are "not run (sandbox)",
  never "failed". Exclude known socket-server tests from Codex runs.
- The orchestrating session commits; Codex never does.
- After each round, list leftover sandbox processes and stop stale ones under
  the checker's authority (section 8).
- Codex review findings are claims: check them cheaply from the evidence.
  Spawn a stronger reviewer only when the evidence cannot settle one, and say
  why.
- **Provider safety filter.** If a review trips the provider's safety filter,
  stop and tell the integrator. Never reword a brief to get around it. Ask
  the integrator which review route to use instead (for example, the repo's
  `red-team` agent), and say which models ran.
- Detailed lane mechanics (brief, review prompt structure, fix rounds) are in
  the integrator's process document; defer to it where it is newer.

## 10. Public text and security

The repository is **public**. "Tracked text" means files, commit messages, PR
bodies, issues, and summary lists of entry titles.

**Never put in tracked text**

- the names of deployments, their environments, their organisations or their
  users;
- internal session names, lane names, personal names or account names;
- `/home/<user>` or `/Users/<user>` paths (write `~/.pt/<session>/<lane>` or
  `<repo>/...`);
- any description of how a control, guard, approval, redaction or admission
  check can be bypassed, weakened or wrongly credited, until its fix has
  landed and the maintainer clears it;
- numbers, file:line references or labels of private items.

**How to apply**

- Check every item you take from a private source (ledgers, lane files,
  memory) before it goes into tracked text. Private sources are not always
  marked, so check each item. When unsure, list the entry ids and ask the
  release integrator or the maintainer.
- **Defensive and protective controls.** [M 2026-11-22] There is a meaningful
  split between a defensive control, which reflects a standard the project
  is accountable for and must not deviate from, and a protective control,
  which exists to maintain a standard someone else is accountable for but
  which the project can help with.
  - Defensive (authorisation, path containment, review cards, audit
    integrity): no deviation; gaps are must-fix; findings stay private until
    fixed.
  - Protective (a control that helps users meet a standard they own): a
    documented limit is honest public documentation.
  - Test: does the text describe a way to get something you should not have,
    or a limit on how far we protect users from their own mistakes? Only the
    first stays private. Do not reclassify a control on your own.
- Only the maintainer decides on security risk.
- Never rewrite pushed history to fix a leak without the maintainer's
  decision. Fix the tip files and keep new text clean.
- Faithful history does not relax new text: commit messages stay plain, and
  the integrator checks builders' messages before merging.
- Ported outside scripts are rewritten as repository tools with parameters and
  no personal details.

**Secrets**

- An agent never holds `ELSPETH_JUDGE_METADATA_HMAC_KEY` (AGENTS.md [O1]).
  Signing never runs in CI.
- The operator signs when the package or release is complete (AGENTS.md).
  Record tier-model signature churn in each HANDOFF as an operator
  obligation.
- Stage a signature worklist (the key-free `elspeth-judge` `stage_*` tools)
  only when the maintainer asks for one, and then once, on the final tree.
  Until then, leave signature churn recorded in the HANDOFF and stage
  nothing. [M 2026-11-23] An expected-failure (xfail) signature test is not
  a defect to fix; leave it as it is.
- [M 2026-11-21] There is a key in the `.env` file for agent use. Agents may
  load the provider key from the main checkout's `.env`. Never print, paste,
  log or commit its value; refer to it by variable name. It draws on the same
  budget as acceptance runs. Other environment files stay off-limits.
- The secret scanner rescans every line of a touched file. Mark a false
  positive with `# secret-scan: allow-this-line`. [M 2026-11-20] No-verify is
  by permission only: never pass `--no-verify` unless the maintainer has
  permitted it for that commit. When a hook fails for an environmental reason,
  fix the environment (`scripts/worktree-cleanup.sh --execute --link-venv`).
- Build credential-shaped test values from parts at run time; never write the
  literal.

## 11. Composer invariants and the tutorial

The two composer invariants in AGENTS.md are absolute: the LLM does the job,
and there are no tutorial-special paths. Read them there.

- Any latency or cost change on the rootless or tutorial entry path gets
  per-transition provider-call scrutiny before it lands (AGENTS.md).
- [M 2026-11-20] He wants to make sure the tutorial is not being broken,
  because it is very compelling as it is. The tutorial is the acceptance gate
  for every skill-text or tool-surface change (section 6). A tutorial
  failure is a composer defect, never a reason for a tutorial-only path.
- [M 2026-11-18] Make sure all changes include appropriate guidance in the
  composer. Every HANDOFF names its composer guidance diff, and the skill
  tool-inventory drift check passes. Guidance teaches; it never authors
  structure.
- Model-facing text is written by the design agent of the time (section 9),
  as a set, briefed on the whole issue. The HANDOFF shows before and after,
  the pin's measured change, and the angles considered.
- The static skill names no installation-specific plugin. Installation facts
  go in the overlay or in server hints.
- Server hints to the model are a few lines of facts, never graph data, whole
  nodes or ready tool calls.

## 12. Checklists

**Starting or resuming a lane**

- [ ] Identify your release's integrator by listed name. None? Stop and ask
      the maintainer.
- [ ] Get the base commit and a box slot from the integrator or the custody
      holder.
- [ ] Create a worktree under `.claude/worktrees/`, link `.venv`, and confirm
      `elspeth.__file__` resolves inside it, with both source roots on
      PYTHONPATH (section 6).
- [ ] If the lane runs browser or frontend tests, link
      `src/elspeth/web/frontend/node_modules` from the main checkout, only
      when `package-lock.json` is byte-identical to the main checkout's.
      `--link-venv` does not do this, and a missing module shows up as
      failing tests, not as a setup error.
- [ ] Set `TMPDIR` and basetemp under `~/.pt/<session>/<lane>` (temp only;
      project work stays in the worktree).
- [ ] Write the lane's `run.sh` (lock, cores, workers, stop file, refusals)
      and test its refusals.
- [ ] Write the brief (section 9) into the lane folder.
- [ ] On resume: re-read the lane's latest report, logs and `.exit` files, not
      the resume note.

**Handing off a lane**

- [ ] Commit validated work.
- [ ] After any rebase: ruff over `src/elspeth`, mypy the overlap, rebuild the
      scoped list on the new tip.
- [ ] Red on base on an exported base tree; green on the lane.
- [ ] Scoped tests, the add-by-name tests, the mock gate and the CI lint steps
      verbatim, with exit codes from files.
- [ ] Review done, diff hash unchanged, findings settled.
- [ ] Commit messages checked against section 10.
- [ ] `HANDOFF.md` complete (section 5), including the CHANGELOG entry text.
- [ ] Basetemps pruned on green; no stray processes; release the box slot.
- [ ] Tell the integrator by listed name, with the HANDOFF path.

**Landing a lane (integrator)**

- [ ] Record the HANDOFF in the queue file.
- [ ] Read the production diff in full; check red on base and decisive claims.
- [ ] Confirm the scoped list was built on the current tip.
- [ ] Check commit messages for public-text leaks.
- [ ] `scripts/branch-safety-check.sh --intent merge` in the landing worktree.
- [ ] Confirm with every lead that no run reads the tree; then
      `git merge --no-ff <sha>`.
- [ ] Record the merge SHA and what was not run on that head.
- [ ] `scripts/branch-safety-check.sh --intent push`, then a plain push.
- [ ] Update the queue file.

**Releasing (integrator)**

- [ ] Scope frozen or locked as the maintainer ruled; closing list done.
- [ ] Testcontainer and timing-sensitive selections run; full gate green and
      `frozen=yes`; every red triaged.
- [ ] CHANGELOG section written from the HANDOFF blocks and checked against
      section 10.
- [ ] Leak review of the release delta; README and CHANGELOG review; fixes
      applied.
- [ ] Release SHA pushed to `release/**`; PR open; push CI green on the PR
      head; tutorial gate clean.
- [ ] Maintainer's authorisation to merge; ordinary merge.
- [ ] Main's post-merge CI green; tag the release merge; image workflow green.
- [ ] Hygiene: `scripts/worktree-cleanup.sh`, merged branches, Dependabot;
      list anything dirty or unmerged for the maintainer.
- [ ] Hand over to the next integrator; declare done to all peers.

**Taking over as integrator**

- [ ] The maintainer has appointed you, or confirmed the handover to you.
- [ ] Read the handover document, the previous integrator's ledger, this
      book, and memory.
- [ ] Confirm box custody: who holds it, and when it passes.
- [ ] Rebuild the inventory from git: every unlanded branch and worktree,
      every HANDOFF received and its state. Sweep with `git cherry`.
- [ ] List the standing limits carried over, and the questions open with the
      maintainer.
- [ ] Tell every lane lead, by listed name, that you are the integrator and
      where HANDOFFs go.
- [ ] Confirm with the maintainer whether you are now the memory editor
      before you edit memory (Reading, not his words: the editor is the
      custody holder). Once confirmed, tell peers to relay rulings to you.
