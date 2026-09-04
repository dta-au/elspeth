# Re-verification of the multi-replica resume brief's measured anchors

**Brief under review:** `docs/plans/2026-09-03-multi-replica-resume-brief.md`

> **SUPERSEDED ON FIGURES, current on reasoning.** This is a snapshot taken at
> `release/0.8.0` @ `77537c8de`. Mainline has since moved to `cbae1ef0c` (two
> major changes: `51b43a770`, `cbae1ef0c`). Live values now: **496 behind / 74
> ahead**, **843 mainline-changed files**, **82 touched on both sides**, **33
> merge conflicts**, **`SESSION_SCHEMA_EPOCH` = 50**, **nine worktrees**, and
> mainline is **PUSHED** (`origin/release/0.8.0 == HEAD`), which retires the
> public/private inversion §2 of the brief describes. Read the per-claim
> *reasoning* and *commands* below as current; read every *number* as needing
> a re-run. Rebaseline summary: `2026-09-03-multi-replica-resume-brief.md` §0.

(801 lines, written 2026-09-03, tracked on mainline since `91816d0f3`).

**Measured:** 2026-09-04 (local, UTC+10) — i.e. 2026-09-03T16:00–16:15Z.

**Tree state at measurement.** Mainline moved **twice** relative to what this
task was briefed with:

| | sha | commit date (local +10) |
|---|---|---|
| brief's own base | `f7d741d2f` | 2026-09-03T21:46:18 |
| this task's stated base | `91816d0f3` | 2026-09-03T23:02:00 |
| **actual HEAD when I measured** | `77537c8de` | 2026-09-04T01:59:20 |

`git status --short` = empty (0 lines) at start and at end of the run; `git
rev-parse HEAD` re-checked at the end and still `77537c8de`. **Every mainline
measurement below is pinned to the explicit sha `77537c8de`, not to the branch
name** — the checkout is shared with seven live lanes and moved once during
this session already.

The platform branch **did not move**: `feature/deferred-platform-recovery` ==
`origin/feature/deferred-platform-recovery` == `a2176dfe2` — the same sha the
brief measured. **Consequence: every platform-only anchor in the brief is
frozen and can only be wrong if the brief mis-measured it. It did not — all
platform-side anchors re-derive exactly.** Everything that moved, moved because
mainline moved.

**Method note on the counts.** Where a count changed I re-ran the same command
against the brief's own base `f7d741d2f` as a control. All three of the §2 item 1
counts reproduce **exactly** at `f7d741d2f` (405 / 758 / 77). The brief was
correct as written; the numbers are simply attached to a moving mainline. Treat
them as *velocity*, not as errors.

---

## Summary

Counts are derived by tallying the 45 anchor entries enumerated in §15, not
asserted independently.

| Status | Count |
|---|---|
| STILL_TRUE | 34 |
| MOVED | 10 |
| FALSE_AS_WRITTEN | 0 |
| UNMEASURABLE | 1 |
| **total anchors** | **45** |

**No *substantive* anchor in the brief was wrong when written** — every claim
about what the code does, what exists and what does not, reproduces exactly.
Three **line-number citations** in §2's invariants were off by one at the time
of writing (see §5.1): the files are byte-unchanged since `f7d741d2f`, so those
are authoring slips, not drift. Per the task's instruction they are recorded
STILL_TRUE with the corrected line, but §13 should not be read as claiming the
brief was flawless to the digit.

The ten MOVED anchors are concentrated in four places: the header block, §2 item
1's integration-debt counts, §3's workspace table, and H11 — plus one stray,
H1's `replay=` line count.

---

## 1. Header block (brief lines 8–14)

### 1.1 Mainline sha and ahead/behind — MOVED

```
$ git rev-parse HEAD
77537c8de8b7ce59cc916f0c5b3ee6ae419a8ce8
$ git rev-parse --abbrev-ref HEAD
release/0.8.0
$ git status --short
(empty)
$ git rev-parse origin/release/0.8.0
8ef46621b14b5b7d732a814f25a81c9e7457b5e4
$ git rev-list --left-right --count origin/release/0.8.0...release/0.8.0
0	8
$ git rev-list --count origin/release/0.8.0..HEAD
8
```

- **Brief:** `release/0.8.0` @ `f7d741d2f`, clean, **5 ahead** of
  `origin/release/0.8.0` @ `8ef46621b`, unpushed.
- **Now:** `release/0.8.0` @ **`77537c8de`**, clean, **8 ahead / 0 behind** of
  `origin/release/0.8.0` @ `8ef46621b` (origin unchanged), still unpushed.
- The 5-ahead figure was correct at `f7d741d2f` (that commit is the 5th in the
  list below).

The eight unpushed commits:

```
$ git log --oneline origin/release/0.8.0..HEAD
77537c8de Merge fix/guided-phase2: guided 2.1/2.2 — goal-first start, no planner run without an intent
d64cc6106 feat(composer/guided): ask the goal first; never plan without an intent
91816d0f3 docs: restore the compiler design to tracked specs, add the multi-replica resume brief
f7d741d2f docs(specs): record why "span" was rejected, and point the proposal at its spec
521ae78df docs(chroma_sink): cite the method that exists
6cc499a46 build(pdf): render the project-control document set
a85dfcfcd docs(specs): sso rev2.8 — apply the verified remainder of the round-3 review
eabe4d52e docs(specs): sso rev2.7 — close the four blocking defects from the round-3 panel review
```

Note the inversion the brief flags is **unchanged and now sharper**: the
platform branch is public at origin while mainline is 8 unpushed commits ahead.

### 1.2 Platform branch sha, and origin parity — STILL_TRUE

```
$ git rev-parse feature/deferred-platform-recovery
a2176dfe225bd9d56d5eed642f7a8aff0a9485d2
$ git rev-parse origin/feature/deferred-platform-recovery
a2176dfe225bd9d56d5eed642f7a8aff0a9485d2
```

Identical on origin, exactly as the brief states. The branch has not moved.

### 1.3 Merge base — STILL_TRUE

```
$ git merge-base release/0.8.0 feature/deferred-platform-recovery
7cd2fc6db08714386bfb7e9d1ddd9b012f8c589d
```

`7cd2fc6db`, unchanged. (Mainline's new commits are descendants, so the base
does not move.)

### 1.4 Divergence counts — MOVED (one side)

```
$ git rev-list --left-right --count release/0.8.0...feature/deferred-platform-recovery
408	74
```

- **Brief:** platform 74 ahead, **405** behind.
- **Now:** platform **74 ahead** (STILL_TRUE), **408 behind** (MOVED, +3).

### 1.5 `git diff --shortstat <merge-base> <platform>` — STILL_TRUE

```
$ git diff --shortstat 7cd2fc6db feature/deferred-platform-recovery
 228 files changed, 70362 insertions(+), 8260 deletions(-)
```

Exactly the brief's "228 files, +70,362 / −8,260". Both endpoints are fixed, so
this anchor cannot drift until the platform branch moves.

---

## 2. §2 item 1 — the integration debt (brief line 106)

**Brief:** "The integration debt is 405 commits and 758 mainline-changed files
since `7cd2fc6db`, with 77 files touched on both sides."

### At today's HEAD `77537c8de` — MOVED

```
$ git rev-list --count 7cd2fc6db..77537c8de
408
$ git diff --name-only 7cd2fc6db 77537c8de | wc -l
789
$ comm -12 <(git diff --name-only 7cd2fc6db 77537c8de | sort) \
           <(git diff --name-only 7cd2fc6db a2176dfe2 | sort) | wc -l
80
```

### Control run at the brief's own base `f7d741d2f` — reproduces exactly

```
$ git rev-list --count 7cd2fc6db..f7d741d2f
405
$ git diff --name-only 7cd2fc6db f7d741d2f | wc -l
758
$ comm -12 <(git diff --name-only 7cd2fc6db f7d741d2f | sort) \
           <(git diff --name-only 7cd2fc6db a2176dfe2 | sort) | wc -l
77
```

| | brief (@`f7d741d2f`) | today (@`77537c8de`) | delta |
|---|---|---|---|
| commits | 405 | **408** | +3 |
| mainline-changed files | 758 | **789** | +31 |
| files touched on both sides | 77 | **80** | +3 |

Platform-changed files since the base: **228** (unchanged, matches the shortstat
file count).

**Planning consequence:** the debt grew by 31 mainline files and 3 both-sides
files in ~4 hours of wall clock. The brief's own line 759 ("The branch is 405
behind and the gap is widening") is empirically confirmed at ~1 commit/80 min.
Do not paste 405/758/77 into a plan — re-derive at the frozen sha you actually
integrate from, and state that sha beside the number.

---

## 3. §2 item 2 — the five deliverables, derived from code

All five derive against `a2176dfe2`, which has not moved. Deliverables are
listed **by title**, as the brief insists.

### 3.1 "Persistent session-operation fence" — STILL_TRUE

```
$ git grep -l "session_operation_fences" a2176dfe2 -- src/ | wc -l
5
$ git grep -l "session_operation_fences" a2176dfe2 -- src/
a2176dfe2:src/elspeth/web/coordination/repository.py
a2176dfe2:src/elspeth/web/coordination/run_recovery_authority.py
a2176dfe2:src/elspeth/web/sessions/models.py
a2176dfe2:src/elspeth/web/sessions/schema.py
a2176dfe2:src/elspeth/web/sessions/service.py
$ git grep -l "session_operation_fences" 77537c8de -- src/ | wc -l
0
```

Brief: "appears in 5 platform source files and 0 on mainline." **Exact.** The
same figure is restated at §4 step 7 and is equally exact there.

### 3.2 "PostgreSQL membership and epoch-fenced run ownership" — STILL_TRUE

This is the one the brief calls "the programme's core safety property" and tells
you to derive from code. I measured it three ways.

**Schema exists:**

```
$ git grep -n "web_instances_table\|owner_instance_id\|owner_epoch\|owner_lease_expires_at" \
    a2176dfe2 -- src/elspeth/web/sessions/models.py
…:443:web_instances_table = Table(
…:2189:    Column("owner_instance_id", String, nullable=True),
…:2190:    Column("owner_epoch", Integer, nullable=True),
…:2191:    Column("owner_lease_expires_at", DateTime(timezone=True), nullable=True, index=True),
```

**The two types appear in exactly the four code files the brief names** (plus
three documentation files the brief did not count, correctly, since they are not
code):

```
$ git grep -l -E "RunOwnershipFence|WebInstanceLease" a2176dfe2
a2176dfe2:docs/plans/2026-07-26-finish-deferred-deployment-platforms.md
a2176dfe2:docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md
a2176dfe2:docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.review.json
a2176dfe2:src/elspeth/web/coordination/__init__.py
a2176dfe2:src/elspeth/web/coordination/contracts.py
a2176dfe2:src/elspeth/web/sessions/protocol.py
a2176dfe2:tests/unit/web/coordination/test_contracts.py
```

Per-file occurrence counts: `coordination/__init__.py` 4, `contracts.py` 5,
`sessions/protocol.py` 2, `test_contracts.py` 5. Three of the four code files
are a definition, an export and a protocol declaration; the fourth is that
declaration's own unit test. **Nothing consumes them.**

**Never threaded — measured at each of the three sites the brief names:**

```
$ git grep -n -E "RunOwnershipFence|WebInstanceLease" a2176dfe2 -- src/elspeth/web/sessions/service.py | wc -l
0
$ git grep -n -E "RunOwnershipFence|WebInstanceLease" a2176dfe2 -- '*repository.py' | wc -l
0
$ git grep -n -E "RunOwnershipFence|WebInstanceLease" a2176dfe2 -- 'src/elspeth/web/execution/' | wc -l
0
```

Zero at all three. **The brief's characterisation is exactly right: schema
only.** I widened the third probe from `repository.py` to `*repository.py`
tree-wide and it is still zero — so this is not an artefact of the brief naming
one path among several.

Supporting context, also re-measured (brief §4 step 7):

```
$ git grep -c "SessionOperationContext" a2176dfe2 -- src/  →  files=23 hits=276
$ git grep -c "SessionOperationContext" 77537c8de -- src/  →  0 files
```

"276 source hits across 23 files on the platform branch and zero on mainline" —
**exact**. The context-free-write-path hazard the brief budgets for is intact.

### 3.3 "Cross-replica safety for tickets, composer progress and rate limits" — STILL_TRUE

```
$ git show --stat --oneline 75bd20ca1 | grep -i composer_progress
 .../coordination/composer_progress_mutations.py    | 419 ---------------------
 .../web/test_composer_progress_fencing_postgres.py | 334 ----------------
```

`75bd20ca1` still shows `composer_progress_mutations.py` deleted, still 419
lines, and the 334-line testcontainer file alongside it. Both figures match the
brief (§2 item 2 and §4 step 10). A commit's stat cannot drift; recorded here
because the task asked for it explicitly.

### 3.4 "Audit-first authoritative transitions — outstanding" — UNMEASURABLE

No symbol, file or table name is given for this deliverable, and the title does
not bind to a greppable identifier. I did **not** independently derive it.
What I can state: `a2176dfe2` is byte-identical to what the brief measured, so
whatever the brief derived on 2026-09-03 is unchanged. Anyone who needs this
one confirmed must first pick the code anchor that represents it — that choice
is not recorded in the brief and is a real gap.

### 3.5 "The full two-process failure, cancellation and race matrix — outstanding" — STILL_TRUE

The brief's §4 step 11 precondition (a corpus that "exists on **no** branch)
re-measures to zero on both branches:

```
$ git ls-tree -r --name-only a2176dfe2 | grep -c "tests/testcontainer/web/multiprocess"   → 0
$ git ls-tree -r --name-only 77537c8de | grep -c "tests/testcontainer/web/multiprocess"   → 0
$ git ls-tree -r --name-only a2176dfe2 | grep -c "web-multi-instance"                     → 0
$ git ls-tree -r --name-only 77537c8de | grep -c "web-multi-instance"                     → 0
```

Neither `tests/testcontainer/web/multiprocess/` nor
`.github/workflows/web-multi-instance.yml` exists on either branch. Outstanding,
confirmed.

---

## 4. §2 prohibition — operator signing (brief lines 180–186) — STILL_TRUE

```
$ git diff 7cd2fc6db..feature/deferred-platform-recovery -- config/cicd/enforce_tier_model/ | wc -c
0
```

Zero bytes of output — not "no signature lines", literally an empty diff. The
prohibition still HOLDS on the same evidence the brief cites. (Both endpoints
are fixed shas, so this cannot drift until the platform branch moves.)

---

## 5. §2 invariants (brief lines 199–209)

**Control first:** none of the three files changed between the brief's base and
today.

```
$ git diff --stat f7d741d2f 77537c8de -- \
    src/elspeth/engine/orchestrator/source_iteration.py \
    src/elspeth/engine/barrier_coordination.py \
    src/elspeth/engine/executors/collector.py
(empty output — unchanged)
```

So any line-number discrepancy below is an off-by-one in the **brief**, not
drift in the tree.

### 5.1 Quarantine dispatch — STILL_TRUE, one anchor exact, two off by one

```
$ grep -n "is_quarantined" src/elspeth/engine/orchestrator/source_iteration.py
666:                        if source_item.is_quarantined:
$ grep -n "check_aggregation_timeouts(\|handle_coalesce_timeouts(\|handle_row_union_timeouts(" \
    src/elspeth/engine/orchestrator/source_iteration.py
…
693:                            timeout_result = check_aggregation_timeouts(
702:                                handle_coalesce_timeouts(
711:                                handle_row_union_timeouts(
…
```

| brief | today | verdict |
|---|---|---|
| `:666` dispatches `is_quarantined` | **`:666`** exactly; `self._quarantine_router.route(` at `:667` | STILL_TRUE, exact |
| "fires the three sweeps at lines 694-716" | sweep block is **`693`–`716`** (`check_aggregation_timeouts(` opens at 693; `handle_row_union_timeouts(` closes at 716) | STILL_TRUE, **start off by one**: read 693-716 |
| "`continue`s at 731" | **`continue` is at `:732`**. Line `731` is the `break` inside the `shutdown_event` guard | STILL_TRUE, **off by one**: read 732 |

The semantics are unchanged: quarantine → route → clear `operation_id` → the
three sweeps in idle-pump order → progress → `restore_source_iteration_context`
→ latch/shutdown check → `continue`. A quarantined row is still a consumed-row
boundary that fires the timers and never becomes downstream row input.

**Warning for whoever restates this invariant:** `731` and `732` are adjacent
and one of them is a `break` that exits the source loop on shutdown. Citing 731
as the `continue` is the kind of one-line slip that turns into a wrong claim
about shutdown handling. Use 732.

### 5.2 The collector arm — STILL_TRUE (and the brief's file path needs a caveat)

```
$ grep -n "A quarantined member already holds\|and is skipped" src/elspeth/engine/barrier_coordination.py
951:            # A quarantined member already holds (FAILURE, QUARANTINED_AT_SOURCE)
952:            # and is skipped; a second write would trip
$ grep -n "Surviving members' TERMINAL outcomes are NOT written here" src/elspeth/engine/executors/collector.py
1089:    # 2. Surviving members' TERMINAL outcomes are NOT written here — under the
```

- `barrier_coordination.py:948-955` → the cited text lands at **951–952**,
  inside the brief's range. **STILL_TRUE.**
- `executors/collector.py:1089-1095` → the settlement-seam threading note begins
  at **1089** exactly. **STILL_TRUE, exact.** (The *write* of the quarantined
  member's `(FAILURE, QUARANTINED_AT_SOURCE)` terminal is separately at
  `collector.py:1049-1054`, `path=` at `:1052` — worth knowing, because a
  restatement that cites only 1089-1095 points at the comment explaining the
  skip, not at the code performing the write.)

**Path caveat, not a correction:** the brief writes `barrier_coordination.py`
bare, next to `executors/collector.py` which is repo-relative-ish. The file is
`src/elspeth/engine/barrier_coordination.py` — **not** under
`src/elspeth/engine/orchestrator/`, where the neighbouring `source_iteration.py`
citation lives. `git ls-tree -r --name-only 77537c8de | grep barrier_coordination`
returns exactly two paths (`src/elspeth/engine/barrier_coordination.py`,
`tests/unit/engine/test_barrier_coordination.py`), so there is no ambiguity —
but I looked in `orchestrator/` first and found nothing, and the next reader
will too. Spell the path.

---

## 6. The xfail census (brief lines 210–221) — STILL_TRUE, exactly

```
$ grep -rn "pytest.mark.xfail\|@mark.xfail" tests/ elspeth-lints/ src/
tests/unit/architecture/test_state_engine_catalog_contract.py:1280:        "@pytest.mark.xfail(reason='expected failure')\n"
tests/unit/architecture/test_state_engine_catalog_contract.py:1283:        "@pytest.mark.xfail(reason='unexpected pass')\n"
```

Two hits, both **string literals** inside a fixture that synthesises pytest
output, at exactly the two line numbers the brief names — `:1280` and `:1283`.
**Zero real decorator sites.**

```
$ grep -rn "xfail" tests/ src/ elspeth-lints/
… (34 lines; the only executable xfail call is:)
tests/unit/elspeth_lints/test_allowlist_loader_unification.py:214:    pytest.xfail("\n".join(lines))
```

The one live, data-dependent `pytest.xfail()` is at **`:214`** exactly as
stated — it fires only when the lints fingerprint baseline has drifted (see the
docstring at `:183`).

Everything else the wide grep returns is prose in docstrings ("formerly a strict
xfail", "no skips, no xfail") or the string key `"xfailed"` in result-count
dicts and JUnit-shape fixtures. Nothing to investigate.

**All three of the brief's xfail anchors — the zero, and both line numbers, and
the third file's line number — are exact.** The directional rule (any xfail
other than the accepted one, any skip, any warning delta, any collection shrink
requires investigation) is unaffected by anything I measured, and U4 remains
open: I did **not** run the suite, so there is still no baseline.

---

## 7. §3 Workspace (brief lines 231–262)

### 7.1 Worktree table — MOVED (three rows)

```
$ git worktree list
<repo>                                            77537c8de [release/0.8.0]
<repo>/.claude/worktrees/guided-remediation       d64cc6106 [fix/guided-phase2]
<repo>/.claude/worktrees/identity-sprint          91816d0f3 [identity-sprint/phase1-contracts-epoch]
<repo>/.claude/worktrees/interim-integrate        4b34d972c [interim-merge-target]
<repo>/.claude/worktrees/lane-elspeth-0077cb7789  284260506 [lane/elspeth-0077cb7789]
<repo>/.claude/worktrees/lane-elspeth-24d79f64cd  284260506 [lane/elspeth-24d79f64cd]
<repo>/.claude/worktrees/lane-elspeth-68721c71d7  0f7c2b53c [fix/strany-coalesce-reachability-facts]
<repo>/.claude/worktrees/lane-strany-toolresult   3ad3a32f7 [strany/tool-result-envelope]
<repo>/.claude/worktrees/recon-diagnosis          680afe64b [fix/review-reconciliation-diagnosis]
```

| worktree | brief | today | verdict |
|---|---|---|---|
| the main checkout | `release/0.8.0` @ `f7d741d2f`, clean | `release/0.8.0` @ **`77537c8de`**, clean | **MOVED** |
| `guided-remediation` | `fix/guided-phase2` @ `d25a1fc64` | `fix/guided-phase2` @ **`d64cc6106`** | **MOVED** |
| **`identity-sprint`** | *(absent from the brief)* | `identity-sprint/phase1-contracts-epoch` @ **`91816d0f3`** | **NEW ROW** |
| `interim-integrate` | `interim-merge-target` @ `4b34d972c` | same | STILL_TRUE |
| `lane-elspeth-0077cb7789` | `lane/elspeth-0077cb7789` @ `284260506` | same | STILL_TRUE |
| `lane-elspeth-24d79f64cd` | `lane/elspeth-24d79f64cd` @ `284260506` | same | STILL_TRUE |
| `lane-elspeth-68721c71d7` | `fix/strany-coalesce-reachability-facts` @ `0f7c2b53c` | same | STILL_TRUE |
| `lane-strany-toolresult` | `strany/tool-result-envelope` @ `11fc9eb4c` | `strany/tool-result-envelope` @ **`3ad3a32f7`** | **MOVED** |
| `recon-diagnosis` | `fix/review-reconciliation-diagnosis` @ `680afe64b` | same | STILL_TRUE |

**"Seven" is now eight.** The brief says "All seven `.claude/worktrees/*` symlink
`.venv`" and "the eight live worktrees are listed in §3" (counting the main
checkout). Today: **8 under `.claude/worktrees/`, 9 entries in `git worktree
list`.** The new one, `identity-sprint`, is the SSO/identity phase-1 lane.

**The `.venv` symlink rule survives the new arrival — all eight, `readlink`-confirmed:**

```
$ for d in <repo>/.claude/worktrees/*/; do readlink "$d/.venv"; done
<repo>/.venv   (x8 — guided-remediation, identity-sprint, interim-integrate,
                            lane-elspeth-0077cb7789, lane-elspeth-24d79f64cd,
                            lane-elspeth-68721c71d7, lane-strany-toolresult, recon-diagnosis)
```

So §4 step 5 **Arm B's prohibition on `uv sync` now covers eight worktrees, not
seven**, and the shared `.venv` has one more lane depending on it. Update the
count wherever the brief says seven.

### 7.2 `.worktrees/` — STILL_TRUE

```
$ test -d <repo>/.worktrees && echo EXISTS || echo "DOES NOT EXIST"
DOES NOT EXIST
```

Still absent. Any plan step asserting `test -d .worktrees` still fails, and
§3's "`mkdir` the parent first" instruction still applies.

### 7.3 The four named branches — STILL_TRUE (all four, local and remote)

```
$ for b in codex/deferred-platform-completion codex/deferred-interpretation-resolution \
           codex/finish-deferred-deployment-platforms release/0.7.2 \
           recovery/deferred-platform-completion; do
    git rev-parse --verify --quiet "refs/heads/$b"   || echo "$b no local ref"
    git rev-parse --verify --quiet "refs/remotes/origin/$b" || echo "$b no remote ref"
  done
codex/deferred-platform-completion no local ref / no remote ref
codex/deferred-interpretation-resolution no local ref / no remote ref
codex/finish-deferred-deployment-platforms no local ref / no remote ref
release/0.7.2 no local ref / no remote ref
recovery/deferred-platform-completion no local ref / no remote ref
```

All four still do not exist, **local or remote**. I also re-checked the fifth
non-existence the brief records (`recovery/deferred-platform-completion` without
`-alt`, the dangling restore pointer in `c4ff80553`) — also still absent.

`git for-each-ref | grep -i codex` returns only refs under
`refs/codex/{integrate-prs-*, review-prs-*, snapshots, turn-diffs}` — no branch
refs. Matches the brief's line 25–26 claim.

### 7.4 The two non-disposable branches — STILL_TRUE

```
$ git for-each-ref --format='%(refname) %(objectname:short)' | grep -i "recovery/"
refs/heads/recovery/deferred-platform-completion-alt 20a4156c3
refs/heads/recovery/deferred-platform-wip-broken 3f3857d20
```

Both still exist at the recorded shas.

```
$ git branch -r --contains 3f3857d20
(empty)
```

`recovery/deferred-platform-wip-broken` @ `3f3857d20` is **still unpushed and
still on no remote** — §7 action 1 (back it up) is still owed, 1 day later.

```
$ git rev-list --left-right --count a2176dfe2...20a4156c3
2011	1
```

`-alt` is still 1 ahead / 2011 behind `a2176dfe2` — the brief's figure exactly.

```
$ for r in a2176dfe2 77537c8de 3f3857d20; do
    git cat-file -e $r:src/elspeth/web/sessions/interpretation_validation.py && echo PRESENT || echo ABSENT
  done
a2176dfe2  ABSENT
77537c8de  ABSENT
3f3857d20  PRESENT
```

`3f3857d20` remains the **sole copy in git** of
`interpretation_validation.py` — absent from both branches, present only there.
The §7 item 1 hazard is undiminished.

---

## 8. H6 — Epoch collision (brief lines 663–671)

### 8.1 `SESSION_SCHEMA_EPOCH` on each branch — STILL_TRUE, both line numbers exact

```
$ git grep -n "SESSION_SCHEMA_EPOCH = " 77537c8de -- src/elspeth/web/sessions/models.py
77537c8de:src/elspeth/web/sessions/models.py:255:SESSION_SCHEMA_EPOCH = 49
$ git grep -n "SESSION_SCHEMA_EPOCH = " a2176dfe2 -- src/elspeth/web/sessions/models.py
a2176dfe2:src/elspeth/web/sessions/models.py:248:SESSION_SCHEMA_EPOCH = 48
```

Mainline **49** at `models.py:255`; platform **48** at `models.py:248`. Both
values and both line numbers are exactly as the brief records. The collision is
live and unresolved.

### 8.2 `_COORDINATION_HARD_CUT_EPOCH` — STILL_TRUE, with a fact the brief leaves implicit

```
$ git grep -rn "_COORDINATION_HARD_CUT_EPOCH" a2176dfe2 -- src/
a2176dfe2:src/elspeth/web/sessions/schema.py:34:_COORDINATION_HARD_CUT_EPOCH = 48
a2176dfe2:src/elspeth/web/sessions/schema.py:366:    if SESSION_SCHEMA_EPOCH != _COORDINATION_HARD_CUT_EPOCH:
a2176dfe2:src/elspeth/web/sessions/schema.py:367:        _schema_error("coordination schema epoch mismatch", expected=..., actual=...)

$ git grep -rn "_COORDINATION_HARD_CUT_EPOCH" 77537c8de
77537c8de:docs/plans/2026-09-03-multi-replica-resume-brief.md:664:(`models.py:248`, with `_COORDINATION_HARD_CUT_EPOCH = 48` at `schema.py:34`);
```

Platform: `= 48` at `schema.py:34`, exactly as stated. **On mainline the symbol
does not exist in any source file** — the only tree-wide hit is the brief
quoting itself.

**This sharpens H6 into something the brief understates.** The platform branch
carries a hard assert at `schema.py:366-367` that fails closed when
`SESSION_SCHEMA_EPOCH != _COORDINATION_HARD_CUT_EPOCH`. Mainline's epoch is 49.
So the moment mainline is merged into the platform branch, that assert fires at
schema init unless `_COORDINATION_HARD_CUT_EPOCH` is moved in the same change.
The brief says "Settle the number with the operator before merging" — correct,
and the reason is now concrete and citable: **the platform branch will refuse to
start, not silently mismatch.**

### 8.3 The live compatibility triple — STILL_TRUE (with a branch qualifier the brief omits)

```
$ git grep -rn "SQLITE_SCHEMA_EPOCH = " 77537c8de -- src/
77537c8de:src/elspeth/core/landscape/schema.py:346:SQLITE_SCHEMA_EPOCH = 36
$ git grep -rn "SQLITE_SCHEMA_EPOCH = " a2176dfe2 -- src/
a2176dfe2:src/elspeth/core/landscape/schema.py:346:SQLITE_SCHEMA_EPOCH = 36

$ git grep -rn "WEB_COORDINATION_PROTOCOL_VERSION" a2176dfe2 -- src/
a2176dfe2:src/elspeth/web/coordination/contracts.py:24:WEB_COORDINATION_PROTOCOL_VERSION = 1
  (+ export at coordination/__init__.py:6,30; doc reference at contracts.py:7)
$ git grep -rn "WEB_COORDINATION_PROTOCOL_VERSION" 77537c8de -- src/
(exit 1 — no match)
```

The brief says "live values are `(48, 36, 1)`". That is **correct, and it is
specifically the PLATFORM triple**:

| component | platform `a2176dfe2` | mainline `77537c8de` |
|---|---|---|
| `SESSION_SCHEMA_EPOCH` | 48 | **49** |
| `SQLITE_SCHEMA_EPOCH` | 36 | 36 |
| `WEB_COORDINATION_PROTOCOL_VERSION` | 1 | **does not exist** |

**On mainline the triple cannot be formed at all** — the third component is a
platform-only symbol. Anyone re-checking "(48, 36, 1)" from a mainline checkout
will get one wrong value and one `NameError`, and may conclude the brief is
wrong. It is not; it is describing the platform branch. Write the branch beside
the triple.

### 8.4 `receipt_contracts.py` coupling — STILL_TRUE, on **both** branches

```
$ git grep -n "SESSION_SCHEMA_EPOCH" 77537c8de -- src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py
…:19:from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
…:144:    f"session_epoch_{_ROLLBACK_BASELINE_SESSION_EPOCH}_to_{SESSION_SCHEMA_EPOCH}"
…:159:            "session_epoch": SESSION_SCHEMA_EPOCH,
```

Identical hits at identical line numbers on `a2176dfe2`. The import is at
**`:19`** and the rollback-baseline receipt string bakes the epoch at
**`:144`**, both exactly as the brief cites (it says `:144-145`; the f-string
occupies 143-145 with the interpolation on 144).

**One addition the brief does not record:** there is a **third** consumption at
`:159`, `"session_epoch": SESSION_SCHEMA_EPOCH` inside a receipt payload dict.
So the epoch is baked into the receipt in two places, not one. Whoever settles
the epoch number must update both, and the file is byte-identical on the two
branches — meaning it will merge cleanly and silently while its baked value
changes underneath.

---

## 9. H10 — Test-run semantics (brief lines 732–744)

### 9.1 `addopts` on each branch — STILL_TRUE

Mainline `77537c8de:pyproject.toml`, `addopts` opens at line 450 and ends:

```
    "-m", "not slow and not stress and not performance and not testcontainer and not live_provider",
    # …comment block explaining ~17 HOURS serial vs ~20 minutes at -n 12…
    "-n", "12",
]
```

Platform `a2176dfe2:pyproject.toml`, same opening line 450, ends:

```
    "-m", "not slow and not stress and not performance and not testcontainer and not live_provider",
]
```

**Exactly as the brief states.** Mainline ends `"-n", "12"`; the platform branch
ends at the marker expression. A bare `pytest tests/` still means two different
things, and `-n 0` on the platform branch is still a no-op rather than an
override. Pass `-n 12` explicitly there.

### 9.2 AGENTS.md line counts — STILL_TRUE, exact

```
$ git show 77537c8de:AGENTS.md | wc -l
287
$ git show a2176dfe2:AGENTS.md | wc -l
180
```

287 mainline / 180 platform — **both exact**, and the difference is still 107
lines. The instruction "Read `AGENTS.md` from mainline for the whole resume"
stands unchanged.

---

## 10. H11 — Loomweave (brief lines 746–753) — MOVED, materially

`mcp__loomweave__project_status_get` (called; `loomweave analyze` **not** run by
me):

```
db_path                       <repo>/.weft/loomweave/loomweave.db
db_size_bytes                 995,856,384  (≈996 MB)
git_sha                       77537c8de8b7ce59cc916f0c5b3ee6ae419a8ce8
latest_run.analyzed_at_commit 77537c8de8b7ce59cc916f0c5b3ee6ae419a8ce8
latest_run.status             completed
latest_run.started_at         2026-09-03T15:59:22.283Z
latest_run.completed_at       2026-09-03T16:00:36.578Z
staleness                     fresh
worktree_dirty                false
counts                        entities 84,368 · edges 336,488 · subsystems 212 · findings 340
```

| brief | today | verdict |
|---|---|---|
| single ~996 MB DB at `<repo>/.weft/loomweave/loomweave.db` | 995,856,384 bytes at that path | STILL_TRUE |
| no worktree has a `.weft` directory | `ls -d .claude/worktrees/*/.weft` → exit 2, no matches | STILL_TRUE |
| latest run completed **2026-09-03T10:11:46Z at `8ef46621b`** | completed **2026-09-03T16:00:36Z at `77537c8de`** | **MOVED** |
| `project_status_get` reports **stale** | reports **`fresh`** | **MOVED** |
| any `mcp__loomweave__*` result about `a2176dfe2` is a mainline result | unchanged — one DB, indexed at mainline HEAD | STILL_TRUE |

### The analyze that the brief said needs an operator go-ahead has already run

**This is the finding on this item, and it is the one thing in this report that
is not merely a number moving.**

The brief's H11 closes: *"Get an operator go-ahead before running `loomweave
analyze` — from the main checkout it would clobber the shared index seven live
lanes depend on."*

That run has happened. The evidence is unambiguous and three-way consistent:

- `latest_run` started `2026-09-03T15:59:22Z` and completed `16:00:36Z` — which
  in the repo's local timezone (UTC+10) is **2026-09-04T01:59:22 → 02:00:36**.
- The DB file mtime is **Sep 4 02:01** (`ls -l`).
- Current local time when I measured: **2026-09-04T02:08**. So the analyze
  finished **~8 minutes before this measurement** — i.e. at this session's start,
  matching the note in my task brief that "a background analyze was started from
  the main checkout at session start".
- It indexed commit `77537c8de`, which was itself only created at
  `2026-09-04T01:59:20` local — 2 seconds before the analyze started.

**Reporting this as instructed, plainly: a `loomweave analyze` was started from
the main checkout at this session's start, and it completed. The brief requires
an operator go-ahead for exactly that action, because the index is a single
shared 996 MB DB that eight live lanes (now eight, not seven — see §7.1) read
from. I did not run it and I have no record of a go-ahead having been given.**

Two mitigating facts, measured rather than assumed, which is why I am not
calling this damage:

1. The run took **74 seconds** (15:59:22 → 16:00:36), not the hours a full
   re-extraction takes. That is an incremental analyze over the two new commits,
   not a rebuild.
2. It **improved** the index rather than corrupting it: staleness went `stale` →
   `fresh`, and the indexed commit advanced from `8ef46621b` (which was 3
   commits behind even at the brief's writing) to current HEAD. Entity and edge
   counts are populated and SEI is `populated: true`.

So the practical effect on the seven-plus lanes is that Loomweave answers are
now *more* current than the brief describes. The process point stands regardless:
**the gate in H11 was crossed without a recorded go-ahead, and H11's remedy
sentence should be updated to say the index is fresh at `77537c8de` rather than
stale at `8ef46621b`.** The substantive hazard H11 exists to prevent —
*Loomweave answers about the platform branch are silently mainline answers* — is
**completely unaffected** and remains true: there is still exactly one DB, still
no `.weft` in any worktree, and it is still indexed at a mainline commit.
`a2176dfe2` has never been indexed.

---

## 11. §4 step 5 — `rfc8785` canary (brief lines 340–344) — STILL_TRUE, all three legs

```
$ <repo>/.venv/bin/python -c "import importlib.metadata as m; print('rfc8785', m.version('rfc8785'))"
rfc8785 0.1.4
exit=0
```

Installed in the shared venv at **0.1.4** — the brief's "verified in the shared
venv" holds.

```
$ git show 77537c8de:pyproject.toml | grep -n "rfc8785"
56:    "rfc8785>=0.1.4,<0.2",  # Deterministic JSON serialization
$ git show a2176dfe2:pyproject.toml | grep -n "rfc8785"
56:    "rfc8785>=0.1.4,<0.2",  # Deterministic JSON serialization
```

Pinned **identically on both branches at `pyproject.toml:56`** — the brief's
line reference is exact and unchanged despite 408 mainline commits.

```
$ git show 77537c8de:uv.lock | grep -n -A2 'name = "rfc8785"'
3956:name = "rfc8785"
3957-version = "0.1.4"
3958-source = { registry = "https://pypi.org/simple" }
$ git show a2176dfe2:uv.lock | grep -n -A2 'name = "rfc8785"'
3956:name = "rfc8785"
3957-version = "0.1.4"
3958-source = { registry = "https://pypi.org/simple" }
```

**`uv.lock:3956-3957` on both branches, byte-identical, same line numbers** —
exactly as the brief records. The canary will not move across the integration.

---

## 12. H1 — the `state_revert` ordering defect (not on the task list; measured because it is merge-blocking)

The task did not ask for H1. I measured it anyway for one reason: the **two new
mainline commits are both guided-composer work** (`d64cc6106 feat(composer/guided):
ask the goal first; never plan without an intent`, and its merge `77537c8de`),
and H1's subject file lives in that same area — making it the anchor in the
brief most likely to have drifted, and §7 action 3 makes it merge-blocking.

### 12.1 Control — the file did not change

```
$ git diff --stat f7d741d2f 77537c8de -- src/elspeth/web/sessions/routes/composer/state.py
(empty output — unchanged)
```

Despite the two new commits landing in the guided composer, `state.py` is
byte-identical to what the brief measured.

### 12.2 All five H1 line anchors — STILL_TRUE, every one exact

```
$ grep -n "_surface_reverted_interpretation_reviews\|replay=\|only_missing_evidence\|metadata_ is None" \
    src/elspeth/web/sessions/routes/composer/state.py
 91:async def _surface_reverted_interpretation_reviews(
102:    if state_record.metadata_ is None:
115:        only_missing_evidence=True,
642:        await _surface_reverted_interpretation_reviews(
655:        replay=_replay,
683:    await _surface_reverted_interpretation_reviews(
```

| brief anchor | today | verdict |
|---|---|---|
| surfacer defined at `state.py:91-116` | opens at **91** | exact |
| `_replay` calls the surfacer at `:642-646` | `await …` at **642** | exact |
| passed as `replay=` at `:650-655` | `replay=_replay,` at **655** | exact |
| early return when `metadata_ is None` at `:102-103` | **102** | exact |
| `only_missing_evidence=True` at `:115` | **115** | exact |

`grep -n "after_verified"` over `state.py` returns **nothing** — confirming the
defect the brief describes: the surfacing call rides `replay=` with no
`after_verified=` anywhere in the file. **H1 is intact, unfixed, and its fix
instructions (including the fixture requirements at `:102-103` / `:115`) apply
verbatim.**

**One fact the brief does not record:** there is a **second** call to
`_surface_reverted_interpretation_reviews` at **`state.py:683`**, outside the
`_replay` closure. The brief's fix ("move the surfacing call out of `_replay`
and pass it as `after_verified=`") therefore has to reconcile with an existing
non-replay call site 28 lines below. Whoever implements H1 should read 642 and
683 together, not 642 alone.

### 12.3 The `replay=` count — MOVED (22 → 23)

The brief (line 598) writes: *"a bare `grep -rn "replay=" src/elspeth/` returns
22 lines, which includes keyword arguments in overload stubs and internal
forwarding, so state the exclusion rule beside any count taken from it."*

```
$ grep -rn "replay=" src/elspeth/ | wc -l
23
$ git grep -c "replay=" f7d741d2f -- src/elspeth/   →  guided.py:10  guided_chat_atomic.py:5  guided_plan.py:5  state.py:1  sessions.py:1   (total 22)
$ git grep -c "replay=" 77537c8de -- src/elspeth/   →  guided.py:11  guided_chat_atomic.py:5  guided_plan.py:5  state.py:1  sessions.py:1   (total 23)
```

**MOVED: 22 → 23.** The single new occurrence is in
`src/elspeth/web/sessions/routes/composer/guided.py` (10 → 11), introduced by
the new guided commit. `state.py` still contributes exactly 1.

This is precisely the number the brief warned would be misread, so it is worth
being blunt: **the brief's "22" is now 23, and the brief's own caveat is the
reason that matters** — anyone auditing `replay=` call sites for purity now has
one more site to classify, and it is in the file that already contributes 11 of
the 23.

### 12.4 "No purity gate over `replay=` callables exists anywhere in `tests/`" — reported, not confirmed

```
$ grep -rln "replay=" tests/ | wc -l
1
$ grep -rn "replay=" tests/
tests/unit/web/sessions/routes/test_guided_operations.py:76,93,116,134,159,197,231,257,277,295,316
```

Exactly one test file references `replay=`, at 11 sites, all of them per-test
fakes (`replay=lambda _locator: _never()`, `replay=lambda _locator: _response(…)`).
None is an architecture-level gate asserting the *class* of `replay=` callables
is side-effect-free.

**I am not upgrading this to a confirmation.** The brief's claim is a negative
over an unbounded set — a purity gate could be spelled without the token
`replay=` at all (via an AST walk, a fixture name, or a different keyword). What
I measured is narrower and is all I will assert: *no test file other than
`test_guided_operations.py` mentions `replay=`, and none of its 11 sites is such
a gate.* Settling the brief's stronger claim needs a search the brief does not
specify.

### 12.5 §4 step 7's platform-only guard test — STILL_TRUE, exact

```
$ git grep -n "test_terminal_replay_never_acquires_session_authority" a2176dfe2
a2176dfe2:tests/unit/web/sessions/routes/test_guided_operations.py:135:async def test_terminal_replay_never_acquires_session_authority(monkeypatch) -> None:
$ git grep -n "test_terminal_replay_never_acquires_session_authority" 77537c8de
77537c8de:docs/plans/2026-09-03-multi-replica-resume-brief.md:406:   (the brief quoting itself — no test)
```

Present on the platform branch at **`test_guided_operations.py:135`** — the exact
line the brief cites — and **absent from mainline's entire tree**, where the only
hit is the brief's own text. The instruction "preserve the platform-only guard
test … there is nothing to fall back on" holds unchanged.

---

## 13. What I did NOT measure

Stated so nobody reads absence as confirmation.

- **§2 item 2 deliverable 4, "Audit-first authoritative transitions."** No code
  anchor is given and the title does not bind to a symbol. See §3.4.
- **Any test run.** No `pytest` was invoked. U1 (whether the Task 5 gates pass on
  the platform branch) and U4 (the suite baseline) remain exactly as open as the
  brief leaves them. **Worker count is therefore not applicable to anything in
  this report** — no count here comes from a test run.
- **The filigree tracker.** Every H9 / §7 tracker claim (ticket states,
  assignees, comment numbers, `work_stale_list` membership) is untouched. The
  brief's §7 footer records `elspeth-4d6c0dd0f5` as `in_progress`, unassigned,
  `is_ready: false` **as measured 2026-09-03** — that is a tracker fact and
  tracker facts move independently of git. Re-measure before acting on it.
- **H2, H3, H5, H7, H8.** Not in scope for this pass and not re-measured.
  (H1 **was** measured — see §12 — because its subject file sits in the guided
  composer area both new mainline commits touch, and §7 action 3 makes it
  merge-blocking. It is fully intact.)

---

## 14. Bottom line for planners

1. **No substantive anchor was wrong when written.** Every claim about what the
   code does, what exists and what does not, reproduces at the brief's own base.
   The brief's opening assertion — that everything was measured against the live
   tree on 2026-09-03 — survives audit. The one qualification: **three
   line-number citations in §2's invariants were off by one when written** (§5.1),
   in files that are byte-unchanged since `f7d741d2f`. Authoring slips, not drift.
2. **Ten anchors moved, all because mainline moved twice in ~4 hours.** Header
   sha, ahead-count, behind-count, the three §2 item 1 counts, the workspace
   table, the two Loomweave freshness facts, and H1's `replay=` line count
   (22 → 23).
3. **Everything derived from `a2176dfe2` is frozen and exact** — the five
   deliverables, the signing prohibition, the epochs, `addopts`, AGENTS.md line
   counts, `rfc8785`. The platform branch has not moved.
4. **The core safety property is confirmed as the brief describes it.**
   `RunOwnershipFence` / `WebInstanceLease` are in four code files, three of
   which are a definition, an export and a protocol declaration, and the fourth
   is that declaration's test. Zero threading into `service.py`, any
   `*repository.py`, or `web/execution/`. Schema only.
5. **Three edits the brief should take:** `continue` is at `source_iteration.py:732`
   not 731 (731 is a `break`); the sweep block starts at 693 not 694; and
   `barrier_coordination.py` lives at `src/elspeth/engine/`, not under
   `orchestrator/`.
6. **Two additions:** `receipt_contracts.py` consumes `SESSION_SCHEMA_EPOCH` at
   `:159` as well as `:144`; and the platform branch's `schema.py:366-367` will
   **hard-fail at schema init** on the epoch mismatch the moment mainline merges —
   which makes H6 a blocking merge defect, not a number to tidy up.
7. **One process flag:** a `loomweave analyze` ran from the main checkout at this
   session's start (74 s, incremental, completed, index now fresh at
   `77537c8de`). H11 requires an operator go-ahead for that action. Reported, not
   remediated. §7.1's worktree count is now eight, so the shared `.venv` and the
   shared index each have one more dependent lane than the brief records.
8. **H1 is fully intact and still unfixed** — all five line anchors exact, no
   `after_verified=` anywhere in `state.py`, and a second surfacer call at
   `:683` the brief does not mention that the fix must reconcile with. §7
   action 3 (fix before merging) is unchanged and still owed.

---

## 15. Full anchor enumeration

The 45 entries the summary counts. `ST` = STILL_TRUE, `MV` = MOVED,
`UM` = UNMEASURABLE.

| # | § | anchor | verdict |
|---|---|---|---|
| 1 | hdr | mainline `release/0.8.0` @ `f7d741d2f`, clean | MV → `77537c8de`, clean |
| 2 | hdr | 5 ahead of `origin/release/0.8.0` @ `8ef46621b`, unpushed | MV → 8 ahead / 0 behind, origin unchanged |
| 3 | hdr | platform `a2176dfe2`, identical on origin | ST |
| 4 | hdr | merge base `7cd2fc6db` | ST |
| 5 | hdr | platform 74 ahead | ST |
| 6 | hdr | platform 405 behind | MV → 408 |
| 7 | hdr | shortstat 228 files, +70,362 / −8,260 | ST |
| 8 | 2.1 | 405 commits of debt | MV → 408 |
| 9 | 2.1 | 758 mainline-changed files | MV → 789 |
| 10 | 2.1 | 77 files touched on both sides | MV → 80 |
| 11 | 2.2 | `session_operation_fences` 5 platform src / 0 mainline | ST |
| 12 | 2.2 | `RunOwnershipFence`/`WebInstanceLease` in exactly 4 code files | ST |
| 13 | 2.2 | never threaded into `service.py` / execution / `repository.py` | ST (0/0/0) |
| 14 | 2.2 | `web_instances_table` + `runs` `owner_*` columns exist | ST |
| 15 | 2.2 | `75bd20ca1` deletes `composer_progress_mutations.py`, 419 lines | ST |
| 16 | 2.2 | "Audit-first authoritative transitions" outstanding | **UM** — no code anchor given |
| 17 | 2.2 | two-process matrix outstanding; multiprocess corpus on no branch | ST (0/0) |
| 18 | 2 | `git diff MB..platform -- config/cicd/enforce_tier_model/` empty | ST (0 bytes) |
| 19 | 2 | `source_iteration.py:666` dispatches `is_quarantined` | ST, exact |
| 20 | 2 | three sweeps at `:694-716` | ST, **read 693-716** |
| 21 | 2 | `continue`s at `:731` | ST, **read 732** (731 is `break`) |
| 22 | 2 | `barrier_coordination.py:948-955` collector arm | ST (text at 951-952) |
| 23 | 2 | `executors/collector.py:1089-1095` settlement seam | ST, exact |
| 24 | 2 | zero real `pytest.mark.xfail` decorator sites | ST |
| 25 | 2 | two string literals at `test_state_engine_catalog_contract.py:1280`,`:1283` | ST, exact |
| 26 | 2 | `pytest.xfail()` at `test_allowlist_loader_unification.py:214` | ST, exact |
| 27 | 3 | workspace worktree table (7 + main) | MV → 8 + main; 3 rows moved, `identity-sprint` new |
| 28 | 3 | all `.claude/worktrees/*` symlink `.venv` to main | ST (now 8 of 8) |
| 29 | 3 | `<repo>/.worktrees/` does not exist | ST |
| 30 | 3 | 4 named branches do not exist (local **and** remote) | ST |
| 31 | 3 | `recovery/…-wip-broken` @ `3f3857d20` unpushed, sole copy | ST |
| 32 | 3 | `recovery/…-alt` @ `20a4156c3` 1 ahead / 2011 behind | ST |
| 33 | H6 | `SESSION_SCHEMA_EPOCH` 48 platform `:248` / 49 mainline `:255` | ST, both exact |
| 34 | H6 | `_COORDINATION_HARD_CUT_EPOCH = 48` at `schema.py:34` | ST (platform-only symbol) |
| 35 | H6 | live triple `(48, 36, 1)` | ST — **it is the platform triple**; mainline cannot form it |
| 36 | H6 | `receipt_contracts.py:19` imports, `:144-145` bakes the epoch | ST (+ third use at `:159`) |
| 37 | H10 | mainline `addopts` ends `-n 12`; platform ends at marker expr | ST |
| 38 | H10 | AGENTS.md 287 mainline / 180 platform | ST, exact |
| 39 | H11 | latest loomweave run 2026-09-03T10:11:46Z at `8ef46621b` | MV → 16:00:36Z at `77537c8de` |
| 40 | H11 | `project_status_get` reports stale | MV → `fresh` |
| 41 | H11 | one ~996 MB DB, no `.weft` in worktrees, platform answers are mainline | ST |
| 42 | 4.5 | `rfc8785 == 0.1.4` in venv, `pyproject.toml:56`, `uv.lock:3956-3957` both | ST, all three |
| 43 | H1 | `state.py:91-116` / `:642-646` / `:650-655` / `:102-103` / `:115` | ST, all five exact |
| 44 | H1 | `grep -rn "replay=" src/elspeth/` returns 22 lines | MV → 23 (`guided.py` 10→11) |
| 45 | 4.7 | platform-only guard test `test_guided_operations.py:135` | ST, exact; absent on mainline |

**Tally: ST 34 · MV 10 · FALSE_AS_WRITTEN 0 · UM 1 · total 45.**
