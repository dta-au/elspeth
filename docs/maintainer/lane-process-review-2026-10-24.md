# Lane process review — 2026-10-24

Scope: are the agent lane processes fit for purpose for a single maintainer with
agents coding? Read-only pass: no tests, no edits under review, git read through
`--no-optional-locks`, canonical scripts in dry-run mode. "Q" is the Codex
batch-qualification worktree and "R" the Codex `2ab4` worktree (outside this checkout).

## Verdict

The repository's core guardrails are cheap and sound. The canonical scripts take
0.1–4.5 s to dry-run. Claims Must Be Measured, Test Verification, Commit Hygiene,
the whole-tree gates and the delivery posture all prevent named incidents. The
**lane layer is not fit for purpose.** lane-manager is the only tool that requires
work to be in commits, and it was last used on 2026-09-08. The orchestrator's state
machine is decorative: 106 of its 131 lanes are still "pending" and it holds 9 step
rows in total. Everyone does follow one lane convention, "lane state goes in
gitignored `.claude/lanes/`", but it has no retention rule and does not forbid
source code there. So that directory became the place where work lives. It holds
**1,287 full copies of `src/elspeth`, about 215 GB across three lane directories,
and nothing has ever been pruned.** Only 6 of 321 entries are referenced by any
tracked file. No rule anywhere puts a time limit on uncommitted work. The only
script that inspects worktrees ranks IN-USE above DIRTY, so a live agent's tree
with 534 uncommitted entries reports only as "in use". The standing dispatch grant
calls under-dispatching "the real risk" and ranks token cost third. The measured
result is evidence volume far larger than code volume. The fix is small: one ADD
(a work-at-risk check); two rules (source lives only in commits; lane directories
are disposable, with a retention limit); cut the orchestrator and the red-team
trigger; shrink lane-manager to its commit check; temper the dispatch grant; and
for 0.8.10, collapse the plan's per-unit ceremony while keeping the parts that
require commits.

## Decisions table

| Element | Prevents (cited) | Cost (measured) | Verdict |
|---|---|---|---|
| Canonical scripts rule (AGENTS § Canonical scripts) | Improvised `worktree remove --force`, venv clobber, `tail`-masked suites (script headers cite 54,325 transcript Bash calls) | Dry runs 4.52 s / 0.12 s / 1.51 s | **KEEP** |
| `worktree-cleanup.sh` | Data loss from forced removal (header incidents) | 4.52 s; 59 worktrees classified | **KEEP + ADD** age columns; report DIRTY alongside IN-USE |
| `branch-safety-check.sh` | Wrong-branch commits, staged artefacts, HMAC key in env, wrong-tree imports | 0.12 s; 11 checks | **KEEP** |
| `full-suite-gate.sh` | Unrecorded exits, unfrozen trees, wrong-checkout imports | 1.51 s dry; default `--stages ruff,pytest` is a known trap (agent memory); logs default to `/tmp/elspeth-gates`, against AGENTS "never /tmp" | **SIMPLIFY**: default to every stage CI runs; log under the tree |
| `lints` gate stage (keyless trust-tier) | Trust-tier drift | 37 of 37 recorded runs exit 1 by design, mean 158 s | **SIMPLIFY**: replace exit code with corpus set-diff, or drop from default stages; a stage that cannot pass carries no signal |
| Claims Must Be Measured | Inferred "landed/green" claims (d0e62aea41 class) | Seconds per claim | **KEEP** |
| Test Verification Policy | Piped/`tail` false greens | None beyond writing to a log | **KEEP** |
| Commit Hygiene (stage own pathspecs, `commit -- <pathspec>`) | Committing a sibling's index (agent memory 09-09) | None | **KEEP**; add "commit at least every N hours" (below) |
| Delivery posture / ADR-046 | Ceremony on tooling | None | **KEEP**; lanes violate it (below) |
| CONTRIBUTING § whole-tree gates | One `getattr` reddening every sibling (7201beeb7) | ~485 lines to read | **KEEP** |
| Subagent Reporting → `.claude/lanes/<run>/` | Truncated chat reports | 148 GB, 4.4 M files here; no retention | **SIMPLIFY**: notes only; no source, no `.db`; 14-day retention |
| Standing dispatch grant (toolchain.md) | Idle lanes | Drives the evidence volume measured below | **SIMPLIFY**: dispatch proportional to risk; cap evidence per unit |
| lane-manager skill (1,102-line CLI) | Believing a lane's message instead of its git | Unused since 2026-09-08; `verify` runs a full suite per lane and again after every merge | **SIMPLIFY** to "commits ahead of base + red→green", or **CUT** |
| orchestrator skill + `/resume-lanes` | Duplicate writers, quota deaths | 928 + 190 + 99 lines; 131 lanes, 106 pending, 9 steps, last heartbeat 10-16 | **CUT** |
| red-team agent charter | Wrong-reason tests, fail-open gates | Per use | **KEEP** |
| red_team `trigger.py` / `meta_check.py` / hook | Automatic seam review | Hook is `exit 0`; 2 runs ever (2026-09-01), 3 of 4 parse errors; `runs/` empty | **CUT** (dormant code plus tests) |
| `explore-and-pin` | Seam drift | Technique sheet | **KEEP** (out of scope) |
| `dir-bug-sweep` | — | Not on disk in the repo or `.claude/skills` | n/a |
| Codex ceremony (frozen envelopes, source-binding, exact-model reviews, receipts, custody, resource admission) | Nothing enforced | Q: 654 `*-frozen.json`, 235 `source-binding.json`, 1,248 `*-tmp` dirs, 329 `source/` trees | **CUT** (convention only; plan §7b C1/C4 already drops it) |
| 0.8.10 plan §9a execution rules | Snapshot evidence, unreviewed carries | 8 rules; 6 restate AGENTS.md | **KEEP 1, 8; SIMPLIFY** the unit structure around them |
| `.git/hooks/pre-stash` | Nothing: git has no such hook | Still present with a broken `#\!` shebang, although AGENTS.md says it was removed 2026-09-02 | **CUT** (one `rm`; the claim is stale) |
| **Work-at-risk check** | Today's failure | ~5 s | **ADD** (spec below) |
| **Rule: source lives only in commits** | Snapshot directories in place of branches | None | **ADD** (one AGENTS sentence) |

## What made today's failure possible

1. **Nothing requires commits.** AGENTS.md, CONTRIBUTING.md, toolchain.md and plan
   §9a contain no time bound on uncommitted work (`grep -i 'uncommitted\|wip\|commit
   often'` matches only lane-manager's liveness note). lane-manager `verify` is the
   one mechanism that reads "commits ahead of base". Its run files under
   `.claude/lanes/*.json` are dated 2026-08-29 to 2026-09-08 (one is a dry run). No
   `.lanes/` directory exists and no `*.run.json` exists anywhere. Its docs also
   disagree: SKILL.md says `.lanes/`, toolchain.md says `.claude/lanes/`. Sol never
   used it.
2. **The convention points work into gitignored space.** AGENTS § Subagent Reporting
   and toolchain.md both send lane state to `.claude/lanes/`, which is gitignored
   (`.gitignore:69`), and neither sets a retention rule. `worktree-cleanup.sh` then
   classifies any tree holding `.claude/lanes/` as IGNORED and keeps it. Full
   source trees (`find -path '*/src/elspeth/__init__.py'`): 954 in this checkout's
   lanes, 328 in Q's, 5 in R's. Gitignored source is invisible to `git status`. A dirty-tree check alone would
   have missed all 152 snapshot versions.
3. **The worktree inspector hides dirt behind activity.** In `worktree-cleanup.sh`,
   IN-USE is assigned before DIRTY (class chain at lines 184–206). Q had a live
   Codex process and 534 uncommitted entries, so it reported as IN-USE. The script
   reports no ages.
4. **Timeline (git log, `stat %w`).** Q: last ordinary commit `28e27543e`
   2026-10-22 02:23; snapshot `carrier-receipt-prototype-v1` born 10-23 16:37,
   `p4-owner-prototype-v152` born 10-24 16:27; preserve commits 10-24 16:58–16:59
   (the last: 94 files, +28,243/−3,651). R: last ordinary commit `170ec0ba3`
   10-21 22:52; preserve commit 10-24 16:58 (7 files, +2,895). That is about 62 h
   (Q) and 66 h (R) with no commit; a 12 h warning would have fired about two days
   earlier (inference from these timestamps).
5. **Nothing was ever pruned** (dir mtime ages: 68 entries >30 days, oldest 54 days).
   - `5887-batch-row`: 77.7 GB and 2.9 M files, including 9,619 `.db` copies
     (18.9 GB). Its newest file was written 2026-10-01.
   - `worktree-cleanup-20261020`: 105,797 `.py` files under `preserved/`. That
     cleanup copied worktree contents *into* gitignored lane storage instead of
     committing them.
6. **The policy rewards volume.** toolchain.md: "Under-dispatching is the real risk
   … Token cost is the third priority". The six-hour audit
   (`.claude/lanes/release-0.8.10-plan/last-6h-productivity.md`) measured 16,159
   files and 3.22 GB of output against about 5.8 k lines of src and tests, all in
   snapshots, with 0 maintained `.py` files changed and the main blocker still red.

## Disk and usage measurements

| Location | Size | Files | `.db` | src/ts files | Full src copies |
|---|---|---|---|---|---|
| `.claude/lanes/` (321 entries) | 148 GB (`du -sh`) | 4,416,408 | 58,393 / 61.2 GB | 1,753,647 / 33.3 GB | 954 |
| Q lane `batch-qualification-20261023` | 66 GB | 2,528,513 | 7,310 / 16.9 GB | 1,648,588 | 328 |
| R lane `root-p4-application-20261023` | 1.3 GB | 39,947 | 7 | 27,709 | 5 |

The lanes take about 215 GB, roughly 23% of the 941 GB used on the volume. The
worktrees add `.claude/worktrees` at 23 GB (51 dirs) and the Codex worktrees at
83 GB, which includes Q's lane. By dir mtime, 86 GB of the lanes has not been
touched for more than 7 days. Only 6 of 321 lane entries are referenced from
tracked files (`git grep -o '\.claude/lanes/[^ ]*'`; control: the grep does find
`5887-batch-row`). Spot checks: `test-failures-20261020` holds 17,926 json, 7,859
db and 6,436 py files; `composer-plus-20261020` 8,412 py and 626 ts;
`release-0.8.10-plan` 18 notes (476 KB). Typical lane output is copied trees, copied stores and JSON envelopes. Only the
notes are read again.

The same pattern shows up in today's plan, at smaller scale. The tracked master
plan (470 lines, 53 unit ids, 16 `red-team` assignments in §4) cites "VN §n"
evidence held only in 4,797 lines of gitignored notes that about 10 agents wrote
today. AGENTS § Subagent Reporting says durable deliverables belong under `docs/`.

## Codex ceremony: enforced or convention?

`grep -ril` for `source-binding|frozen.json|rehash|exact-model|resource admission|
review receipt` returns **0 hits** in `scripts/`, `.agents/`, `.claude/agents`,
`.claude/commands`, `.github/`, AGENTS.md and CONTRIBUTING.md. The hits in `src/`,
`tests/` and `elspeth-lints/` are product features, not lane process: judge-signature
`source_binding` (`elspeth_lints/core/allowlist.py`), `scripts/composer_acceptance`,
the graph-review "exact model review receipt" (`web/sessions/service.py:7042`), and
blob and key "custody". The Codex global instructions are a single sentence authorising all subagents. The
envelopes, bindings, snapshot versioning and exact-model review mandates are
**convention that appears only in Codex lane documents**. Plan §7b C1/C4 drops them
correctly. Nothing needs removing from the repo; agents must simply not re-import
them. One exception is not Codex ceremony: the "fencing-gate receipts" (U-46) are a
repo mechanism (`scripts/fencing_inventory.py`).

## Plan §9a (execution rules) — proportionate? would it have prevented today?

The brief says "§7 Execution rules"; in the file, §7 is the rulings and challenged
constraints, and the execution rules are §9a. Rule by rule:
- **Rule 1** ("units are reviewed commit groups on `<TARGET>` … no snapshot
  directories, packets or envelopes as evidence") is the one rule that *would* have
  changed today's outcome. Keep it.
- **Rules 2, 4, 6** restate AGENTS.md; cite it instead. **Rule 5** (own venv
  before `uv run`) is a real incident (10-08); keep it.
- **Rule 7** ("nothing pushed before the single merge") follows the maintainer's
  ruling, but it leaves days of work on one disk. CI triggers only on `main`,
  `master`, `RC*` and `release/**` (`ci.yaml:3–7`), so pushing a `wip/*` ref would
  cost no CI. The repo is public, though, and the security work is not. A local
  commit is the minimum; a private mirror is optional. This is a tradeoff, not a
  reversal.
- **Rule 8** (stop and say so) is good. **Missing:** a time bound on uncommitted work, and any lane-directory retention.

The burden is not the 8 rules. It is 53 units, each carrying hooks, branch-safety,
named tests, fail-before/pass-after proofs and a review record. On top of that sit
16 red-team passes, multi-model reviewer pairs, and three mid-assembly testcontainer
batches (B1–B3) before Q. For a "merge the work" rescue, collapse it:
- the cherry-pick carries (U-02..U-06, U-11) become one commit and one review each,
  with no separate red-team;
- red-team runs only on the Composer authoring-path units (U-29, U-30, U-30b,
  U-22, U-41), the chokepoint class AGENTS.md:358–364 names;
- testcontainer runs on schema/lock units with their own named PostgreSQL tests,
  plus one full serial batch at Q;
- review records are the commit message plus the gate `summary.txt`, not a separate
  document.

## Speed: what dominates wall clock

Measured from 26+ `full-suite-gate` `summary.txt` files under `.claude/lanes/`
since 2026-09-20:

| Stage | Runs | Mean | Max | Non-zero |
|---|---|---|---|---|
| pytest (full) | 26 | 1,920 s (32 min) | 6,940 s | 17 |
| testcontainer (serial) | 20 | 992 s | 1,915 s | 6 |
| lints (keyless) | 37 | 158 s | 300 s | 37 (by design) |
| mypy | 69 | 17 s | 69 s | 3 |

AGENTS.md says the full suite takes about 20 min. The measured mean is 32 min, and
65% of the recorded runs were red. CI (`gh run view`): the green PR #328 run took
134 min wall, 34 jobs, 11.4 runner-hours. The longest jobs are Landscape
protected-formal (64 min) and Testcontainer (41 min); py3.13 shards take 28–39 min
against py3.12's 17–24 min, under `max-parallel: 4`. The `main` push at
`3b28d537b` took 483 min wall, sharing the six self-hosted runners with four
Dependabot PR runs of 474–495 min each, and ended **red** (py3.13 shards 2 and 5,
ACA Bicep bundle, combined coverage).

Unnecessary waits: Sol's 24 per-snapshot gate re-runs and 600 s whole-tree proof
timeouts; lane-manager re-running the full suite on every lane after every merge
(N × 32 min, serial); Dependabot PRs each running the full 11.4-runner-hour CI on
the same host (group them weekly or run a reduced job set); a gate stage (lints)
that always fails; local TC batches that duplicate the merge-push CI (keep one at
Q). Necessary waits: one full local suite and one serial TC batch on the final tree,
then CI on the merge push.

## Minimum process set (single maintainer, agents coding)

1. **Work lives in commits.** Each lane gets one worktree and one branch. Commit WIP
   at every agent stop, and at least every 4 h of edits. Rewriting before merge is
   fine; losing work is not. Source under a gitignored path is a defect, not lane
   state.
2. **Lane directories are disposable.** Notes and logs only: no source trees, no
   `.db` copies (point at the store path instead). Delete them 14 days after the
   branch merges or is abandoned. Durable findings go to tracked `docs/`.
3. **Evidence = exit codes on the tree that will merge.** Use AGENTS Claims Must Be
   Measured and the Test Verification Policy as they stand. Per unit: the targeted
   tests' fail-before/pass-after. Per merge: `full-suite-gate --execute --detach`
   with explicit stages, plus TC when schema, SQL or locks changed.
4. **Review follows risk.** Run one independent review per non-trivial commit group.
   Add red-team only on the chokepoints AGENTS already names. No exact-model
   mandates, receipts or envelopes.
5. **Keep:** the three canonical scripts, Commit Hygiene, the whole-tree gates, the
   composer invariants, delivery posture/ADR-046, and the red-team agent charter.
6. **Add the work-at-risk check** (below) and run it at session start and before any
   "handover".

### The ADD: work-at-risk check (dry-run, read-only, about 5 s)

Extend `scripts/worktree-cleanup.sh`, or add `scripts/work-at-risk.sh`, using the
same `--no-optional-locks` reads. For every registered worktree, report hours since the oldest uncommitted or
untracked file, hours since the last commit, the count of `src/elspeth/__init__.py`
files under the tree's gitignored paths (this catches the snapshots git cannot
see), and the `.claude/lanes/` size. WARN when uncommitted work is older than 12 h, when any gitignored source copy
exists, or when lanes exceed 10 GB. Show DIRTY even when the tree is IN-USE.

A read-only prototype over all registered worktrees found one hit: this checkout,
11 uncommitted paths, oldest 113 h, last commit 67 h (positive control: the 11
paths match the session-start `git status`). Every other tree was clean, matching
`worktree-cleanup.sh` `dirty=0`. On 2026-10-22/23 it would have flagged Q and R within a day. The source-copy probe
would have flagged 1,287 trees today.

## Immediate hygiene (operator go-ahead; ADR-046: ordinary tooling hygiene, no ceremony)

- Prune `.claude/lanes/` entries older than 14 days that no tracked file references
  (about 86 GB by mtime). This includes `5887-batch-row` (77.7 GB, idle since
  10-01) after a check that its branch commits are in git.
- Delete Q's lane after the 0.8.10 carve. Its source is now in preserve commits
  `bfa70bdfe` and `3472a3060`.
- Remove `.orchestrator/lanes.db` with the orchestrator skill; fix
  `.git/hooks/pre-stash` or the AGENTS sentence that says it is gone.

## Limits of this review

Ages use directory mtime, not newest-file mtime, except for `5887-batch-row`.
File-type counts are by extension. Q's review-file count (24,606 `*review*.md`) is
inflated by snapshot copies of tracked docs, so it is not used as a measure. CI
timings come from two runs. The local suite figures cover gate runs recorded since
2026-09-20 on a shared host, so load varied. No test, lint or suite was run for
this review.
