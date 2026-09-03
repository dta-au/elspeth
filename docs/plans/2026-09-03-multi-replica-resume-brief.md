# Deferred platform / DAG reacceptance — resume brief

**Written:** 2026-09-03. **Supersedes:** `HANDOFF-2026-08-02.md` (captured
2026-08-02T23:09:02+10:00), which must not be used for planning. Every anchor
below was measured against the live tree, the live filigree database, and git
history on 2026-09-03.

**Mainline:** `release/0.8.0` @ `f7d741d2f`, clean, **5 commits ahead of
`origin/release/0.8.0` @ `8ef46621b`, unpushed**.
**Platform branch:** `feature/deferred-platform-recovery` @ `a2176dfe2`,
identical on `origin`.
**Merge base:** `7cd2fc6db` (2026-08-30). **Divergence:** platform 74 ahead,
405 behind. `git diff --shortstat 7cd2fc6db a2176dfe2` = 228 files,
+70,362 / −8,260.

---

## 1. What changed since the pause

Read this section before any instruction in the old handoff. Ten of its
statements are no longer true.

**The branch was rebuilt onto a new base, and its name changed.**
`codex/deferred-platform-completion` no longer exists as a ref
(`git for-each-ref | grep codex` returns only `refs/codex/{integrate-prs,
review-prs,snapshots,turn-diffs}`). Its tip `929e5ef1f` survives as an
**ancestor** of `feature/deferred-platform-recovery`, reached through
`4c59c9d02` and the **second** parent of merge `0b676d195`
("merge: recover deferred-platform lane 4c59c9d02 onto feature/unified-lineage
7cd2fc6db"). Mainline is the first parent:
`git rev-list --first-parent a2176dfe2 | grep -c 929e5ef1f` = 0. The lane's
ancestry is complete; some of its **tree content** was resolved to the HEAD
side (see HAZARDS H2).

**The branch was pushed.** `origin/feature/deferred-platform-recovery` ==
`a2176dfe2`, first pushed 2026-09-03. The handoff's "Nothing pushed" guarantee
no longer describes the repository.

**Mainline moved ~400 commits and renumbered its schema.** `release/0.7.2` has
no branch ref anywhere (only tags `0.7.2-RC`, `deployed/0.7.2-RC-190826`).
`SESSION_SCHEMA_EPOCH` is **49** on mainline (`src/elspeth/web/sessions/
models.py:255`) and **48** on the platform branch (same file, line 248) — an
independent collision, not a lag.

**Every commit in the handoff's accept/reject ledger is now on both branches.**
`git merge-base --is-ancestor` returns YES against **both** `f7d741d2f` and
`a2176dfe2` for `41680eb60`, `4f1e76dde`, `512c84444`, `d15ff8efd`,
`0d7a8fec7`, `9ff22e801` and `04ec62a8c`. The handoff's rejections of
`512c84444` and `0d7a8fec7` can no longer be exercised — and need not be: they
arrived carrying their own repairs.

**The release WIP the handoff observed became a commit.** `9ff22e801`
"fix: close three defects in the guided RESPOND replay repair arm"
(2026-08-02 23:39:50, +548/−73 over exactly the six observed paths), hardened
41 minutes later by `04ec62a8c`. Both are ancestors of both branches. The
handoff's instruction to "use the resulting release commit(s)" is discharged.

**All three replay defects are closed at mainline.** Verified in code, not from
commit subjects: `get_proposal_by_committed_state` has **zero** occurrences
across `src/` and `tests/`; `_surfaced_evidence_keys` reads
`list_interpretation_events(status="all")`; `_replay_completed`
(`src/elspeth/web/sessions/routes/guided_operations.py:192-212`) orders replay
→ hash compare → `after_verified`. **But defect 3's mechanism was reintroduced
at a different route** — see HAZARDS H1.

**Both named worktrees are gone, and so is the directory that held one.**
`<repo>/.worktrees/` does not exist.
`.claude/worktrees/deferred-interpretation-resolution` does not exist. The
eight live worktrees are listed in §3.

**Two controlling documents left mainline; a third was silently dropped from
the platform branch.** `7a3606102` (2026-08-30, on mainline) deleted the Wave B
handoff and preplatform sprint from `docs/superpowers/plans/`. Separately,
merge `0b676d195` resolved the deferred-platform plan toward mainline's
**17-task** revision while carrying the platform's **v3** spec forward — so the
platform branch today runs a v3 spec against a v1 plan. See §4 step 1 and
HAZARDS H8.

**The default test invocation changed on mainline only.** `680afe64b`
(2026-09-01) added `"-n", "12"` to `[tool.pytest.ini_options] addopts` in
`pyproject.toml`. The platform branch's `addopts` ends at the marker
expression. A bare `pytest tests/` means different things on the two branches.

**The tracker moved past this document.** `elspeth-4d6c0dd0f5` carries nine
comments written 2026-08-30 to 2026-09-01 (8915, 8916, 8917, 8919, 8925, 8949,
8966, 9075, 9099) recording a later recovery lane: per-cohort merge resolution,
operator rulings 1–5, a 536-failure classification, and open decisions. Those
comments supersede this document's predecessor on branch identity, base pin and
merge order. **Read them.** Note that comments 8915, 8966, 9075 and 9099 all
state the branch is unpushed; that was true at their dates and is false now.

---

## 2. Objective and prohibitions, as they apply now

### The bounded objective

1. **Integrate mainline into the platform branch.** Retargeted from
   `release/0.7.2` to `release/0.8.0` @ `f7d741d2f`. **Nothing in the handoff's
   ledger remains to decline** — its seven named commits (`41680eb60`,
   `4f1e76dde`, `512c84444`, `d15ff8efd`, `0d7a8fec7`, `9ff22e801`, `04ec62a8c`)
   are all in common ancestry. That finding covers those seven commits and no
   more: **the 405-commit integration still requires per-cohort semantic review**,
   and H1 is the standing counterexample of a mainline commit that must not be
   taken as-is. The integration debt is 405 commits and 758 mainline-changed
   files since `7cd2fc6db`, with 77 files touched on both sides.
2. **Finish the outstanding platform deliverables, stated by title.** Task
   *numbers* are deliberately not used here: two plan revisions put different
   work behind the same numbers, and which revision governs is an operator
   decision still open (§4 step 1, §7 item 2). Titles are unambiguous under
   either revision. Measured on `a2176dfe2`:

   - **Persistent session-operation fence** — substantially landed.
     `session_operation_fences` appears in 5 platform source files and 0 on
     mainline.
   - **PostgreSQL membership and epoch-fenced run ownership** — **schema only,
     and this is the programme's core safety property.** `web_instances_table`
     and `runs_table.owner_instance_id / owner_epoch / owner_lease_expires_at`
     exist, but `RunOwnershipFence` and `WebInstanceLease` appear in only four
     files (`src/elspeth/web/coordination/__init__.py`, `.../contracts.py`,
     `src/elspeth/web/sessions/protocol.py`,
     `tests/unit/web/coordination/test_contracts.py`) and are never threaded
     into `service.py`, execution, or `repository.py`. **Derive this one's state
     from code before trusting any plan.**
   - **Cross-replica safety for tickets, composer progress and rate limits** —
     partially present and partially *reverted*: `75bd20ca1` deleted 419 lines
     of `composer_progress_mutations.py` (see step 10).
   - **Audit-first authoritative transitions** — outstanding.
   - **The full two-process failure, cancellation and race matrix** —
     outstanding; this is the same deliverable as objective 3.

3. **Implement the genuine distributed B4-B proof.** Still owed. The gate the
   handoff relied on is gone: `elspeth-9a52eb80f9` was claimed
   2026-08-11T18:44:15Z by `codex-state-engine-task6` and closed
   2026-08-11T20:11:33Z, with Tasks 6–13 not done. Its close evidence
   (`55a8a94f4`, `d2f072a07`, both ancestors of both branches) does not meet the
   substrate criterion: `tests/e2e/recovery/test_registered_process_authority.py`
   spawns real OS processes but is SQLite-only, while the PostgreSQL proofs from
   the same commit
   (`tests/testcontainer/core/test_run_coordination_release_postgres.py`,
   `test_scheduler_lease_eviction_postgres.py`) are single-process
   thread-based and are deselected by the default marker expression. Live
   owners are `elspeth-b5d7aa5655` (named by
   `docs/architecture/adr/041-state-engine-supported-profiles.md:41-42`) and
   `elspeth-eefd990b46` (open). **Do not reopen `elspeth-9a52eb80f9`.**
4. **Regenerate and reaccept the DAG corpus on the combined tree.** Unaltered
   and demonstrably not started: zero platform-side commits since `7cd2fc6db`
   touch `docs/architecture/dag` or `tests/fixtures/dag_scenario_corpus`, and
   the platform's `scenario-corpus/v1/manifest.yaml` is still blob `f2e163e40`
   — identical to the merge base — while mainline's is `9e5b42de8`. The
   acceptance contract is **on-tree** and needs no recovery:
   `docs/architecture/dag/completeness-criteria.md`,
   `docs/architecture/dag/assessment-framework.md`,
   `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`,
   `tests/unit/architecture/test_dag_scenario_corpus_contract.py`.
   `elspeth-ef29ef6ba4` is `in_progress`, unassigned, `blocked_by
   elspeth-cb1053fe46` — a blocker the handoff never named, itself open,
   unassigned and ready.
5. **Stop before provider packaging.** Stated by title so it holds under either
   plan revision: **the Kubernetes recreate base, the kind readiness smoke, the
   ACA workload module, the provider runbooks, and ACA operator acceptance are
   all out of scope.** Both revisions place a "Task 14" boundary somewhere in
   that group; naming the deliverables removes the need to decide which. This is
   the same line prohibition 4 draws, and neither revision's numbering is
   required to apply it.

### The prohibitions

- **Do not push — OVERTAKEN, not lifted.** The platform branch is already
  published at `origin/feature/deferred-platform-recovery` @ `a2176dfe2`. The
  intent survives as: **do not merge or open a PR from
  `feature/deferred-platform-recovery` into `release/0.8.0`** until the corpus
  is reaccepted. Note the inversion — the platform branch is the public one
  while mainline's local `release/0.8.0` is 5 commits ahead of origin and
  unpushed.
- **Do not merge into the release line.** Restated: do not merge into
  `release/0.8.0`. No such merge has occurred. The cost is now 405 commits of
  drift, not 3, so an integration pass must be budgeted **before** the §2 item 2 deliverables,
  not after.
- **Do not perform operator signing — HOLDS.**
  `git diff 7cd2fc6db..feature/deferred-platform-recovery --
  config/cicd/enforce_tier_model/` produces no output at all: zero
  `judge_metadata_signature` lines added or removed. This is now repo-wide
  policy (`AGENTS.md` § "Judge-signature stage", custody rule [O1]) rather than
  a lane rule. The branch's last commits (`54ce1f9cf`, `a2176dfe2`) touch
  elspeth-lints tooling and staged-bundle state, not signatures.
- **Do not begin provider packaging — HOLDS**, read as **Tasks 14–16**: the
  maintained claim, the machine-readable profiles, and the public documentation
  transition. It does **not** prohibit Task 13's local build-and-compile; the
  17-task plan's own Task 14 Step 4 says to "merge Tasks 2–13 as an ACA release
  candidate only". State this explicitly in any lane brief so item 2 and this
  prohibition are not read as contradictory. Measured: no bicep, kustomize,
  kubernetes or container-apps path exists on either branch, and
  `docs/reference/deployment-platforms.md` still reads "Runtime contract only;
  no maintained bundle in this release".

### Two invariants to carry forward

- **The quarantine semantic is unchanged and is now pinned in code.** A
  quarantined row is a consumed-row boundary: it triggers the aggregation,
  coalescing and row-union timer sweeps and does not become membership or
  downstream row input. `src/elspeth/engine/orchestrator/source_iteration.py:666`
  dispatches `is_quarantined` to `QuarantineRouter.route`, fires the three
  sweeps at lines 694-716, and `continue`s at 731. Regressions:
  `tests/unit/engine/orchestrator/test_source_iteration_quarantine_sweep.py`,
  `tests/integration/pipeline/orchestrator/test_quarantine_deadline_progression.py`.
  Add the collector arm when restating it: a quarantined member holds
  `(FAILURE, QUARANTINED_AT_SOURCE)` and is skipped by the settlement seam
  (`barrier_coordination.py:948-955`, `executors/collector.py:1089-1095`).
- **The xfail rule is directional only, and its anchor is gone.** The "593
  passed, 0 xfailed, 3 known quorum warnings" baseline lived in the deleted
  Wave B handoff. Static census today: **zero** real `pytest.mark.xfail`
  decorator sites — the two grep hits are string literals at
  `tests/unit/architecture/test_state_engine_catalog_contract.py:1280` and
  `:1283`. The only live xfail is data-dependent:
  `tests/unit/elspeth_lints/test_allowlist_loader_unification.py:214` calls
  `pytest.xfail()` when the lints fingerprint baseline has drifted. The handoff
  named four signals, not one, and they are restated because "deltas" is not a
  synonym for them: **any xfail other than the one the developer accepted, any
  skip, any warning delta, and any collection shrink still requires
  investigation.** Do not enforce a literal count until re-baselined (§5 U4).

- **Verification gates for any work harvested in step 8** (from the handoff, and
  otherwise unrecorded): focused and broad authority suites, Ruff, format, mypy,
  and `git diff --check`.

---

## 3. Workspace

### Exists

| Thing | State |
|---|---|
| the main checkout | `release/0.8.0` @ `f7d741d2f`, `git status --short` empty |
| `.claude/worktrees/guided-remediation` | `fix/guided-phase2` @ `d25a1fc64` |
| `.claude/worktrees/interim-integrate` | `interim-merge-target` @ `4b34d972c` |
| `.claude/worktrees/lane-elspeth-0077cb7789` | `lane/elspeth-0077cb7789` @ `284260506` |
| `.claude/worktrees/lane-elspeth-24d79f64cd` | `lane/elspeth-24d79f64cd` @ `284260506` |
| `.claude/worktrees/lane-elspeth-68721c71d7` | `fix/strany-coalesce-reachability-facts` @ `0f7c2b53c` |
| `.claude/worktrees/lane-strany-toolresult` | `strany/tool-result-envelope` @ `11fc9eb4c` |
| `.claude/worktrees/recon-diagnosis` | `fix/review-reconciliation-diagnosis` @ `680afe64b` |
| `feature/deferred-platform-recovery` @ `a2176dfe2` | the integration target; pushed |
| `recovery/deferred-platform-completion-alt` @ `20a4156c3` | 1 ahead / 2011 behind `a2176dfe2`; adds only the handoff doc |
| `recovery/deferred-platform-wip-broken` @ `3f3857d20` | **unpushed**, ancestor of nothing, sole copy of the interpretation slice |

All seven `.claude/worktrees/*` symlink `.venv → <repo>/.venv`
(`readlink`-confirmed). Several lanes are live in them.

### Does not exist

- `<repo>/.worktrees/` — the directory itself, so any plan step
  asserting `test -d .worktrees` fails today.
- `.worktrees/deferred-platform-completion` and
  `.claude/worktrees/deferred-interpretation-resolution` — both handoff
  worktrees.
- Branches `codex/deferred-platform-completion`,
  `codex/deferred-interpretation-resolution`,
  `codex/finish-deferred-deployment-platforms`, `release/0.7.2`.
- `recovery/deferred-platform-completion` (**without** `-alt`) — cited by
  commit `c4ff80553` as the restore source for a deleted test. That pointer is
  already dangling.

### Two branches that are not disposable

- **`recovery/deferred-platform-wip-broken` @ `3f3857d20`.** The only copy in
  git of `src/elspeth/web/sessions/interpretation_validation.py` and
  `tests/unit/web/sessions/test_interpretation_validation_inputs.py`
  (`git cat-file -e` fails for that path on both `a2176dfe2` and `f7d741d2f`).
  `git branch -r --contains 3f3857d20` is empty. Its parent is `64b7d144e`, the
  base the handoff names. The 14 tracked paths reproduce the handoff's capture
  byte-for-byte: `git diff 64b7d144e 3f3857d20 -- <14 paths> | sha256sum` =
  `baa76f8bbc31059e18d0aee8cf551151ecbffbd4a5417481e10cef1927676ea4`, 1,827
  insertions / 602 deletions.
- **`recovery/deferred-platform-completion-alt` @ `20a4156c3`.** All its code is
  already in the platform branch. Its sole unique content is a 6-line addition
  to the handoff doc — the 23:12 custody-audit paragraph recording that another
  line was editing the release worktree. Everything else in it exists at
  `a2176dfe2:docs/superpowers/plans/2026-08-02-multi-replica-platform-pause-handoff.md`
  (blob `6f026fb01`, the earlier 23:09 revision).

### What the resumer must create

A dedicated worktree for any broad gate run. `mkdir` the parent first if using
the plan's `.worktrees/` convention — it does not exist. See §4 step 5 for the
two provisioning arms; the choice determines which environment proof is valid.

---

## 4. Resume sequence

Eleven steps replace the old eleven. Deletions are stated with their reason so
nobody re-derives them.

**1. Assemble the controlling documents — and settle which plan governs.**
The handoff's step 1 named three documents; all three pointers are broken, and
all are recoverable from mainline's own history.

- Wave B handoff: `git show 7a3606102^:docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-handoff.md` (26,427 bytes).
- Preplatform sprint: `git show 7a3606102^:docs/superpowers/plans/2026-07-26-dag-corpus-wave-b-preplatform-sprint.md` (37,017 bytes).
- The **27-task** deferred-platform plan (27 *numbered* tasks; `grep -c "^## Task"`
  returns **35** because of lettered subtasks 2A/2B/8A/8B/9A/12B/12C/19A — a
  count of 35 headings means you have the right document, not the wrong one):
  blob `1e62a12cc`, live at
  `recovery/deferred-platform-completion-alt` and
  `recovery/deferred-platform-wip-broken` under
  `docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md`.
  Recover it with `git show fcd5df055:docs/plans/2026-07-26-finish-deferred-deployment-platforms.md`
  (blob `1e62a12cc`) — it is reachable from those branches and needs no archaeology.

`docs/plans/2026-07-26-finish-deferred-deployment-platforms.md` on both branches
is blob `659b5a34c` — the **17-task** original, differing from its creation blob
only by the `docs/superpowers → docs/plans` rename in `8548a5e29`. The
coordinator's own tree at `929e5ef1f` carried the 27-task revision and had no
`docs/plans/` copy at all. **Decide with the operator whether the 27-task plan
is restored to `docs/plans/` or formally superseded before reading any task
number.** Note the pair is also inconsistent: the platform branch carries the
**v3** spec (`docs/specs/...-design.md`, blob `97ec21fe8`, 1,582 lines,
"Design, v3") against the v1 plan, and mainline carries the original spec (blob
`dfc073a31`, 395 lines).

**2. [DELETED] "Confirm the coordinator is clean at a commit descended from
`929e5ef1f`."** Unexecutable and misleading: `929e5ef1f` is an ancestor of
`a2176dfe2` but its tree content was resolved to the HEAD side (HAZARDS H2), so
the check would pass while the deliverable is absent.

**3. [DELETED] "Confirm the interpretation WIP hashes; investigate drift."**
Unexecutable as written. Two of three hashes reproduce exactly from
`3f3857d20`; the third —
`src/elspeth/web/sessions/interpretation_validation.py`, which carries the
chosen correction — hashes `da8a9b85…` against the recorded `6a8ef93a…`. The
2026-08-02 bytes are unrecoverable (§5 U2). Review `3f3857d20`'s version on its
own merits instead.

**4. [DELETED] "Fetch `origin/release/0.7.2` and wait for a clean committed
release state."** The ref does not exist and the condition was met on
2026-08-02 at 23:39:50. The immutable release anchor for the replay repairs is
`9ff22e801`, hardened by `04ec62a8c`.

**5. Provision the test environment — two arms, pick one deliberately.**
`rfc8785 == 0.1.4` is verified in the shared venv and pinned identically on both
branches (`pyproject.toml:56`, `uv.lock:3956-3957`); the two branches' locks
differ by exactly one line (the project's own version string), so this canary
will not move across the integration.

- **Arm A — a fresh worktree with its own environment** (required for
  evidence-grade runs per
  `docs/architecture/state_engine/assessment-program.md:365-367`). Create the
  worktree, assert `test -z "$(readlink .venv)"` so you cannot sync through a
  symlink, then
  `env -u VIRTUAL_ENV uv sync --python 3.13 --frozen --all-extras`. The
  `--python 3.13` pin is the one real defect in the handoff's step 6: bare sync
  selected CPython 3.14.3 once and produced nine spurious failures
  (`docs/architecture/state_engine/assessments/2026-08-12-0239/evidence.md:63-70`).
  Then the original proof — `sys.prefix`, Python, pytest, Ruff, mypy resolving
  lexically from that `.venv`, plus `rfc8785 == 0.1.4`.
- **Arm B — an existing `.claude/worktrees/*`.** Do **not** sync: all seven
  symlink `.venv` to the main checkout and several lanes share it. Use
  `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src <repo>/.venv/bin/python
  -m pytest …` and verify **both** `elspeth.__file__` and
  `elspeth_lints.__file__` resolve inside the worktree — `elspeth_lints` lives
  in a separate source root, so `<wt>/src` alone silently measures the main
  checkout.

The handoff's `env -u VIRTUAL_ENV` and `SKIP=check-contracts` conventions both
still hold (`.pre-commit-config.yaml:220` still declares `check-contracts`).

**6. Re-run the replay-defect evidence rather than re-deriving it.** The
handoff's step 4 listed seven required proofs. Five are already covered by
regressions that shipped with `9ff22e801`, all in
`tests/integration/web/composer/guided/test_respond.py`: `:5366` (no-authority,
no-mutation fast replay), `:5399` (exact proposal identity under shared state),
`:5566` (all-status immutable replay after resolution), `:5631` (after state
advancement), `:5725` (partial multi-site surfacing repair), `:5809`
(response-hash rejection before mutation, asserting zero writes). Two are
**not** covered and must be written before this step is signed off:

- Provenance-mismatch rejection. The two refusal branches at
  `src/elspeth/web/sessions/routes/composer/guided.py:2923` ("names a proposal
  that is not a pipeline proposal") and `:2933` ("has incomplete composer
  provenance") appear to have no test coverage — greps for their message strings across `tests/`
  return nothing. Needed: one replay naming a non-pipeline proposal, one whose
  proposal row has NULL composer provenance; both must raise
  `AuditIntegrityError` and write nothing.
- The blob-only shared-state **workflow**. `:5399` proves the ordering and
  identity guarantee but its own docstring disclaims driving the real blob
  acceptance path (HAZARDS H5).

**7. Integrate mainline, and budget the operation-context threading.** Drop the
commit-selection premise — nothing is held back. What survives from the old
step 5 is "preserve platform operation-context authority", and mainline has
grown a context-free write path since the merge base. `SessionOperationContext`
has 276 source hits across 23 files on the platform branch and **zero** on
mainline; `session_operation_fences` appears in 5 platform src files and 0 on
mainline. Mainline's `save_composition_state_with_interpretations` (added by
`370e3bdf0`, 2026-09-01) is defined at `src/elspeth/web/sessions/service.py:9732`
(calling `_prepare_or_create_pending_interpretation_event` at `:9755`), declared
at `src/elspeth/web/sessions/protocol.py:3245`, and called from
`src/elspeth/web/sessions/routes/composer/state.py:786` and `:916`, with a new
repair-surfacer call at `state.py:642-646` (the surfacer itself is defined at
`state.py:91-116`). Each needs a
`SessionOperationLease.acquire` wrapper of the kind the platform branch already
uses, or an explicit exemption ruling. **Expect a type error, not a merge
conflict** — git will not flag this. Also preserve the platform-only guard test
`tests/unit/web/sessions/routes/test_guided_operations.py:135`
(`test_terminal_replay_never_acquires_session_authority`); `git log -S` shows it
was never added to mainline, so there is nothing to fall back on.

**8. Harvest the interpretation slice selectively. Do not merge it.**
`3f3857d20` self-declares broken and was committed `--no-verify`: 15 undefined
names in `pending_interpretation.py:1282-1309` (`suppress`, `partial`,
`ismethod`, `isfunction`, `get_referents`, `is_dataclass`, `fields`,
`MemberDescriptorType`) plus two annotations at `:1517` and `:1729` naming
classes that are defined underscore-prefixed at `:1418` and `:1347`. **The
module is non-functional, though not necessarily unimportable** —
`_forbidden_validation_dependency`'s scanner body references 15 names the module
never imports, so any call raises `NameError`; the two annotations name classes
that do not exist under those spellings. Do not claim an import failure without
proving it with `python -c "import …"`: the undefined names sit inside a function
body, and `from __future__ import annotations` (line 6) makes those annotations
lazy strings. The conclusion is unchanged — harvest, do not merge. Take exactly
three things:

- `src/elspeth/web/sessions/interpretation_validation.py` (355 lines) — the
  closed per-principal DTO, still owed and absent from both branches.
- `tests/unit/web/sessions/test_interpretation_validation_inputs.py` (160
  lines) — hashes exactly to the recorded `8e06c695…`.
- The cleanup-error-primacy change to
  `src/elspeth/web/sessions/routes/interpretation.py`.
  `git rev-list --count 64b7d144e..feature/deferred-platform-recovery -- <that
  file>` is 0, so it applies with no reconciliation.

**Do not replay `pending_interpretation.py`'s scanner hunks.** They reintroduce
`_forbidden_validation_dependency` and `get_referents`, which commit
`c4ff80553` deliberately retired on the platform branch, and would fail the
live pin at `tests/unit/web/sessions/test_operation_fence_wiring.py:234`.
`c4ff80553` executed the handoff's **prohibition** ("do not restore or extend
the general Python object-graph scanner"); it did not deliver the handoff's
**correction**. Scope the DTO to closure and its RED/GREEN proof — the caller
already performs exact process-local nominal rejection
(`test_operation_fence_wiring.py:223`). The handoff's four blocking review
findings still stand as the standing rationale; the P2 TOCTOU finding in
particular survives, because the live catalog and profile registry are still
retained by `_SessionPendingInterpretationValidator`. Then obtain the fresh
independent quality review the handoff required.

**The criteria that review must apply are recorded nowhere else, so they are
restated here verbatim from the handoff.**

*What the closed per-principal DTO must contain:* an immutable
`PluginAvailabilitySnapshot`; detached plugin authority; detached public schema;
detached aliases; detached lowering recipes.

*What it must not retain:* a live `CatalogServiceImpl`; an
`OperatorProfileRegistry`; any Pydantic object; any callable; any iterator; any
module; any database handle.

*The four cleanup-semantics rules the change must satisfy:*

1. before durable commit, the original error remains primary and cleanup may
   only attach context;
2. after the durable event or state receipt exists, fully drain lease close;
3. ordinary cleanup failure is structured-logged and cannot replace the 200
   resolution outcome;
4. cancellation and process-control `BaseException` raised after the drain still
   propagates.

Rule 4 is invisible in a code diff — it is the *absence* of a broadened
`try/except` — and will be silently regressed by any reviewer who does not know
it was a decision.

**9. Re-baseline the Task 5 gates; do not delta against the old counts.** Four
of the handoff's seven category names (unexpected aggregate, connection
violations, unresolved execute, escaped-read) no longer appear in
`tests/unit/architecture/test_session_db_mutation_authority.py`, which is now
7,680 lines and was re-pinned twice post-handoff (`7b402f716`, `3762da105`).
The counts are not commensurable. The still-owed cohort is a designed refactor,
not a two-signature patch — the handoff specifies three parts: consolidate
message, title and state mutation authority across `service.py`, `protocol.py`,
`routes/sessions.py` and `composer/tutorial_service.py`; remove the
optional-context bypasses; and route those writes through typed units of work
with focused regressions. The clearest entry point is one grep: `update_session_title` and `add_message` still take
`session_operation_context: SessionOperationContext | None = None` on the
platform branch (`src/elspeth/web/sessions/service.py:5886`, `:7998`, plus
`:12695`), while `add_message_with_transcript` takes it required — the fix
pattern exists and was simply not applied.

The inventory/domain cohort's five items are recorded nowhere else and are
restated here: LocalAuth sqlite-wrapper provenance RED proofs; propagation
through `LandscapeConnectionProvider`, `Tier1Engine`, `begin_write` and
`write_connection`; wrapper `SELECT` versus DML poisoning; refresh of the exact
read manifest; and classification of each external domain without path-wide
padding. Re-derive the inventory/domain cohort
against `src/elspeth/web/coordination/mutation_connection_registry.py`, which
did not exist when the cohort was written.

**10. Continue the plan through the §2 item 2 deliverables, and record Ruling 2.** Before
resuming what the plans call the tickets/progress/rate-limit task, record that
commit `75bd20ca1` (2026-08-31) removed the cross-replica composer-progress
half under an operator ruling — deleting
`src/elspeth/web/coordination/composer_progress_mutations.py` (419 lines) and
`tests/testcontainer/web/test_composer_progress_fencing_postgres.py` (334
lines). Both plan texts still mandate building those tables. Derive task status
from the tree and tracker only: both plan revisions have **zero** checked
boxes despite substantial implementation.

**11. Re-own and implement B4-B; then reaccept the corpus and stop.** The "must
not claim until startable" gate no longer exists. Re-scope the work onto
`elspeth-b5d7aa5655` or `elspeth-eefd990b46` rather than reopening
`elspeth-9a52eb80f9`. Before accepting any B4-B claim, require registered
production worker **processes** against shared PostgreSQL in a lane the default
gate actually runs — the sprint's own failure condition is "uses threads, one
process, web-session ownership, or a corpus-only lease model", and its
precondition (the two-process PostgreSQL acceptance corpus:
`tests/testcontainer/web/multiprocess/`, `test_multi_instance_*.py`,
`.github/workflows/web-multi-instance.yml`) exists on **no** branch. The corpus
itself still registers the cell as unproven:
`tests/fixtures/dag_scenario_corpus/schema.py:91` lists
`multi-worker-lease-reclaim-late-completion` in `EXPECTED_SCENARIOS`,
`tests/unit/architecture/test_dag_scenario_corpus_contract.py:294` pins its
concurrency dimension as `partial`, and no matching scenario directory exists
under `tests/fixtures/dag_scenario_corpus/v1/`. Then regenerate and reaccept
every DAG evidence item on the combined tree, refresh Loomweave at committed
HEAD, and stop before Task 14 (§2 item 5 — confirm which numbering).

---

## 5. UNVERIFIABLE

Recorded as unknown. Each carries the experiment that would settle it.

**U1 — Whether the Task 5 gates currently pass on the platform branch.** Commit
subjects claim green (`db042b1d1`, `8353991f6`) but `3762da105`'s own message
says "the remaining facet/fencing failures are adjudicated separately".
*Settle:* check the branch out into a worktree per §4 step 5 and run
`tests/unit/architecture/test_session_db_mutation_authority.py`,
`tests/unit/web/sessions/test_static_direct_writers.py`,
`tests/unit/web/sessions/test_operation_fence_wiring.py`,
`tests/unit/web/coordination` to a lane-private log with an explicit
`echo exit=$?`. Do this before any count is written into a checkpoint.

**U2 — The direction and size of the `interpretation_validation.py` drift**
between the 2026-08-02 capture (`6a8ef93a…`) and the 2026-08-06 rescue
(`da8a9b85…`): continued work, partial rewrite, or regression. Searched
exhaustively — every dangling blob hashed with no match, no `refs/stash`, no
reflog for the deleted branch, no copy on disk. *Settle:* only an off-repo copy
(backup, editor undo history, or the codex session transcript that wrote it).
Absent that, review the 355-line file on its merits.

**U3 — How much of the 1,827-line tracked refactor in `3f3857d20` is still
unique.** *Not genuinely unverifiable — settle it before step 8, because it
gates step 8's scope.* Step 8 says take exactly three things; U3 is the question
of whether that list is complete. Comment 8915 on `elspeth-4d6c0dd0f5` records
that the platform branch already performed a symbol-level 3-way merge in which
"the 12 interpretation helpers the lane moved to
`sessions/pending_interpretation.py` now carry HEAD's versions" — but that is a
*report*, not a measurement. *Settle:* diff the 14 files of `3f3857d20` against
`a2176dfe2`. Read-only, no test run, no operator needed.

**U4 — Which xfail the developer accepted, and the current suite baseline.**
*Settle:* ask the developer; and run `pytest tests/` to a file at a frozen HEAD
and take the summary line (passed / xfailed / skipped / warnings) as the new
anchor. Do not report from a grep or a pipe.

**U5 — The "599 focused pytest items" figure and the corpus counts.** The
handoff carries no corpus numbers; `elspeth-ef29ef6ba4`'s `context` field
records "49 registered cases … 599 focused pytest items" as measured
2026-08-11. That is a tracker record, not a live measurement — a live parse of
the manifest gives 51 registered cases. *Settle:* run pytest over the
manifest's registered locators with `-p no:cacheprovider` in a throwaway
worktree and re-parse the manifest at a frozen HEAD.

*Every item above blocks a decision. Two further questions were raised during
reconciliation and are recorded here as footnotes because neither changes any
instruction in this document:* the handoff's "Warpline" was almost certainly a
typo for Wardline — the coordinator's own controlling plan uses `wardline` 33
times and `warpline` not at all — and the handoff states that evidence "was not
used to narrow any full gate", so the answer changes nothing. And whether
`uv sync` writes through a symlinked `.venv` is unmeasured, but §4 step 5 arm B
forbids the sync unconditionally, so both answers yield the same instruction.

---

## 6. HAZARDS

Things that would silently mislead a resumer.

**H1 — Mainline reintroduced defect 3's mechanism at a different route, a month
after the invariant was established.** `src/elspeth/web/sessions/routes/composer/
state.py:642-646`: the `state_revert` `_replay` callable calls
`_surface_reverted_interpretation_reviews`, which reaches
`create_pending_interpretation_event` — an INSERT into `interpretation_events`
(audit-primary). It is passed as `replay=` at `state.py:650-655` with **no**
`after_verified`, so a completed `state_revert` retry writes durably **before**
the hash check. Introduced by `370e3bdf0` (2026-09-01, mainline-only), a month
after `9ff22e801` established the ordering and while `_replay_completed`'s
docstring states "``replay`` MUST be side-effect-free". The other `replay=` call sites are reads or
projections; a bare `grep -rn "replay=" src/elspeth/` returns 22 lines, which
includes keyword arguments in overload stubs and internal forwarding, so state
the exclusion rule beside any count taken from it. **No purity gate over `replay=` callables
exists anywhere in `tests/`.** Fix on mainline before merging 74 commits that
preserve the invariant onto a base that violates it: move the surfacing call
out of `_replay` and pass it as `after_verified=`, mirroring
`guided.py:3446-3447`. Add a `state_revert` analogue of `test_respond.py:5809`
and the missing architecture test. **Specify the fixture, or the regression will
pass while proving nothing:** `_surface_reverted_interpretation_reviews` returns
early when `state_record.metadata_ is None` (`state.py:102-103`) and passes
`only_missing_evidence=True` (`:115`), so a minimal fixture takes the early
return and asserts zero writes. The fixture needs a state record with
**populated metadata** and **missing evidence**, so the path actually reaches
`create_pending_interpretation_event`. This is the H5 failure mode applied to
H1's own fix.

**H2 — `929e5ef1f` is in the ancestry but its deliverable is not in the tree.**
The handoff's one "completed coordinator regression",
`test_audit_row_and_session_are_stamped_under_the_lock`, is absent from
**both** `a2176dfe2` and `f7d741d2f`. Merge `0b676d195` took the unified-lineage
blob for `tests/unit/web/sessions/test_service.py` wholesale (695 insertions /
1,957 deletions on that file). Both branches carry only the weaker
`test_audit_row_is_stamped_under_the_lock_not_at_call_entry`
(`tests/unit/web/sessions/test_service.py:1586`, single assertion). A
`git checkout 929e5ef1f -- tests/unit/web/sessions/test_service.py` will go red:
the coordinator version needs a `session_operation_contexts` fixture that does
not exist on mainline. Restore the two lost assertions by hand and re-run the
mutation proof. Separately, comment 2135's two requested mirror tests
(`test_rejection_stamps_application_time_under_the_lock_not_at_request_entry`,
`test_rejection_fails_closed_when_the_pending_cas_matches_no_row`) grep to zero
hits on both branches — a second apparently-lost coordinator deliverable.

**H3 — The merge message discloses only some of what it resolved.**
`0b676d195`'s body admits taking `composer/*`, `blobs/*`,
`preferences/service.py`, the progress registry, `execution/*` and the composer
routes from HEAD. It does not disclose that
`sessions/service.py`, `sessions/protocol.py` and
`sessions/pending_interpretation.py` match **neither** parent's blob — they were
hand-resolved. Re-audit those three on the merged tree before relying on any
statement about platform operation-context authority.

**H4 — The platform branch carries a stale copy of the old handoff that reads
as current guidance.**
`a2176dfe2:docs/superpowers/plans/2026-08-02-multi-replica-platform-pause-handoff.md`
(blob `6f026fb01`) is the 369-line 23:09 revision — nine lines shorter than the
copy under reconciliation, and **missing the 23:12 hash-drift paragraph**. An
engineer working on that branch will read a falsified pause document and will
not see the warning that another line was editing the release worktree. Delete
or supersede it as part of the merge; the branch also still carries
`docs/superpowers/` (5 files) while mainline renamed that tree at `8548a5e29`,
so a merge will attempt to resurrect retired paths.

**H5 — Tests that do not prove what their names suggest.**
- `tests/integration/web/composer/guided/test_respond.py:5399` — its docstring
  states it "does NOT drive the real blob acceptance path that motivates the
  shared-state case in production". It reproduces the condition, not the
  workflow.
- `test_respond.py:5312`
  (`test_confirm_wiring_ordinary_retry_replays_without_double_surfacing`) still
  ends on a **pending-only** assertion, which permits abandon-and-replace churn
  — the objection raised in comment 2141 and never addressed in that test.
- `tests/unit/web/blobs/test_service.py:928` hand-seeds a composition-state
  shape that **no production writer emits**, so it passes green while
  production 500s (see H7).

**H6 — Epoch collision.** Platform `SESSION_SCHEMA_EPOCH = 48`
(`models.py:248`, with `_COORDINATION_HARD_CUT_EPOCH = 48` at `schema.py:34`);
mainline is **49** (`models.py:255`, set by `07e417a7d`, unrelated composer
work). The plans' pinned compatibility key `(SESSION_SCHEMA_EPOCH,
SQLITE_SCHEMA_EPOCH, WEB_COORDINATION_PROTOCOL_VERSION) == (37, 29, 1)` is dead
in two of three components — live values are `(48, 36, 1)`. Coupled consumer:
`src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py:19` imports
`SESSION_SCHEMA_EPOCH` and bakes it into the rollback-baseline receipt string
at `:144-145`. Settle the number with the operator before merging.

**H7 — An open P1 production defect on mainline whose two prongs have opposite
polarity.** `elspeth-3db5745ba7` (triage / open / P1 / unassigned, filed
2026-08-28) records that `_ACTIVE_RUN_COMPOSITION_COLUMNS` and
`_active_run_pipeline_dict` in `src/elspeth/web/blobs/service.py` read the dead
pre-2026-05 column shape, so every blob delete/replace under a pending/running
run raises `TypeError` (500). **Do not file a duplicate.** The hazard is in the
fix: prong 1 (envelope not unwrapped) is fail-**closed** and currently masks
prong 2 (`sources` never selected, `source` always NULL), which is
fail-**open** — a blob referenced only from a source's options reads as
unreferenced. Adding `_unwrap_envelope` alone downgrades loud-wrong to
silently-wrong. Record this on the ticket before anyone fixes it. The defect is
local to mainline and independently landable; it is not a reason to take the
70,000-line merge.

**H8 — The plan/spec pair on the platform branch is internally inconsistent,
and its checkboxes are meaningless.** The branch implements the v3 design
(`run_start_permits`, `session_operation_fences`, `web_instances` are all in
`a2176dfe2:src/elspeth/web/sessions/models.py`) while carrying the v1 plan,
which mentions `run_start_permits` zero times. Both plan revisions have zero
`- [x]` boxes despite Tasks 1–4 being substantially implemented. Derive status
from code and the tracker only.

**H9 — The tracker misleads in three specific places.**
- `elspeth-9a52eb80f9` is **closed**, so the handoff's "not claimed" gate has
  no live representation. Its own close evidence does not meet its criterion
  (§2 item 3). `docs/architecture/dag/scenario-corpus/v1/manifest.yaml:581`
  still reads "independent-process contention remains owned by
  `elspeth-9a52eb80f9`".
- `elspeth-64c319bf4d` — a release-prep task, still `in_progress`, unassigned,
  `blocked_by elspeth-618b5100b8` — carries comments 2137/2138/2139/2141
  describing the three replay defects and **no follow-up recording their fix**.
  The disposition lives on a different ticket: `elspeth-7a6f0e9c4c` comment
  2142 (2026-08-02T14:20:56Z) names all three under `9ff22e801` and points to
  `04ec62a8c` as the current head. A resumer following the handoff's pointer
  alone reads three open P1 defects and no resolution. The proportionate remedy
  is **one cross-reference comment**, not three new issues and not reopening
  `elspeth-7a6f0e9c4c`.
- `elspeth-f321e3ff21` and `elspeth-245b21351b` are still `fixing` with
  assignee `codex-deferred-platform` and claims that expired 2026-08-04 —
  dead leases under an agent identity that no longer exists, both appearing in
  `work_stale_list`. Neither has a `fix_verification`, so neither can advance.
  Use `work_reclaim`; do not read the live assignee as an active owner.
  `elspeth-3d1d1fcb6c`, the fourth child of `elspeth-b5d7aa5655`, is **closed**
  (2026-08-08, `close_commit release/0.7.2@018f7e4d98`, an ancestor of both
  branches) — remove blob/run consistency from the parent's remaining scope,
  carrying comment 8915's caveat that open decision 5 asks for a PostgreSQL
  two-connection proof of the shared-lock-domain assumption.
- The programme's true closeout node is `elspeth-a5b07ac072` ("Complete
  deployment profiles, documentation, CI gates, and release merge", open, P1,
  unassigned), which the handoff never named. Two of its three blockers —
  `elspeth-2ff97dbc70` and `elspeth-beaf1585a9` — are ready to start now and
  are on nobody's plan.
- Precedent worth knowing: `elspeth-139794d981`, a platform-lane defect, was
  closed WONT_FIX because it "describes the `SessionForkAuthority` refactor that
  exists only on abandoned `codex/deferred-platform-*` WIP branches", with an
  explicit revive-and-fix instruction. Before trusting any "closed" verdict on a
  multi-replica ticket, check whether it was closed on the merits or because the
  code was judged not to exist on HEAD.

**H10 — Test-run semantics differ between the two branches.** Mainline
`addopts` ends `"-n", "12"`; the platform branch's does not, so a bare
`pytest tests/` there is the ~17-hour serial run and `-n 0` is a no-op rather
than an override. Pass `-n 12` explicitly on the platform branch, state the
worker count beside every recorded count, and do not compare a pre-integration
count with a post-integration one. Compounding this: `AGENTS.md` on the platform
branch is **107 lines shorter** than mainline's (287 vs 180) — no `-n 12` guidance, no xdist
flaky-set gotcha, no whole-tree-gates STOP block. **Read `AGENTS.md` from
mainline for the whole resume.** And the flaky set named there
(`e2e/recovery`, `integration/pipeline`, `unit/engine/orchestrator`, tracking
issue `elspeth-0077cb7789`, open) overlaps this programme's subject matter
exactly: re-run any red in those trees with `-n 0` before attributing it to the
integration, and diff failure sets against the same run on the base commit.

**H11 — Loomweave answers about the platform branch are silently mainline
answers.** The index is a single 996 MB DB at
`<repo>/.weft/loomweave/loomweave.db`; no worktree has a `.weft`
directory. The latest run completed 2026-09-03T10:11:46Z at commit
`8ef46621b`. `project_status_get` reports stale. Any `mcp__loomweave__*` result
about `a2176dfe2` is a mainline result. Get an operator go-ahead before running
`loomweave analyze` — from the main checkout it would clobber the shared index
seven live lanes depend on.

---

## 7. What to do first

Three actions precede any code change. The branch is 405 behind and the gap is
widening.

1. **Back up `recovery/deferred-platform-wip-broken`** — push it to origin or
   tag it. It is unpushed, an ancestor of nothing, and the only copy of the
   interpretation slice. Its sibling `recovery/deferred-platform-completion`
   has already vanished the same way and is now a dangling restore pointer in
   `c4ff80553`. Recovery after deletion would need tracker archaeology (the SHA
   survives in comment 8966 on `elspeth-4d6c0dd0f5`); do not rely on that.
2. **Settle which deferred-platform plan governs**, with the operator: restore
   blob `1e62a12cc` to `docs/plans/` or formally supersede it. Nothing
   downstream is safely numberable until this is decided — the same task number
   names different work in the two revisions, and one of them puts provider
   packaging inside the "stop before Task 14" boundary. Raise at the same time
   that `7a3606102` retired the Wave B plans as "implemented plans whose
   Filigree tickets are closed" while `elspeth-ef29ef6ba4` was and remains
   `in_progress`.
3. **Fix the `state_revert` ordering on mainline** (H1) before merging.
   Landing 74 commits that preserve the write-after-verify invariant onto a base
   that violates it would bury the defect inside a 70,000-line diff.

Then run the integration pass — mainline into the platform branch, with the
`SessionOperationContext` threading budgeted per §4 step 7 — **before** any of
the deliverables in §2 item 2.

**The method for that pass is not defined in this document, and that is the
first thing to reconstruct.** It is the largest single action here — 405
commits, 758 files, 77 touched on both sides — and none of the following is
settled: which checkout or worktree to use (the platform branch has none, and
§4 step 5's provisioning arms are written for *test* runs); whether the pass is
a merge, a rebase, or per-cohort resolution; the cohort order, and how to
reconcile it with H3's warning that three files in the previous merge match
neither parent; and how to know the pass is finished, given H10 states pre- and
post-integration counts are not comparable. Nine comments on `elspeth-4d6c0dd0f5`
(8915–9099) record "per-cohort merge resolution" and operator rulings 1–5;
reconstruct the method from them and write it down **before** starting, rather
than discovering the gap with a checkout already in hand.

That ticket's own state, measured 2026-09-03: **`in_progress`, assignee empty,
no claim, no lease, no heartbeat, `is_ready: false`, P1, child of
`elspeth-b5d7aa5655`, last updated 2026-09-01.** It carries the newest handoff
material and is itself unowned — and because `is_ready` is false, a naive
`work_start` will not behave as expected.
