# Per-cohort integration method — mainline into `feature/deferred-platform-recovery`

Reconstructed 2026-09-04 from the nine comments on filigree `elspeth-4d6c0dd0f5`
(8915, 8916, 8917, 8919, 8925, 8949, 8966, 9075, 9099, written 2026-08-30 →
2026-09-01) plus §4 step 7 / §7 / H3 / H6 / H10 of
`docs/plans/2026-09-03-multi-replica-resume-brief.md`.

**Baseline: `release/0.8.0` @ `cbae1ef0c`** (== `origin`, pushed). Rebaselined
2026-09-04 after two major changes landed — `51b43a770` (guided-decline anchor
rebase, P0 `elspeth-ed67eb9d0d`) and `cbae1ef0c` (composer tool-result
envelope). What that rebaseline moved, in one place: **conflicts 31 → 33**
(§4.1), **behind 408 → 496**, **mainline-changed files 789 → 843**, **both-sides
80 → 82**, **`SESSION_SCHEMA_EPOCH` 49 → 50** so H6's collision is now two
epochs wide (§8 D3). The cohort partition, the resolution rule, the operator
rulings and every §8 decision are unaffected in substance — only the arithmetic
moved. **The rate is the finding:** mainline advanced 88 commits and added two
conflicts in a single working day, all of it in M3.

Every number below is measured at that baseline unless labelled *(from comment N)*.
This is a **document, not an execution**. No branch was checked out, nothing was
committed, staged, or pushed. `git merge-tree --write-tree` was used for conflict
measurement — it writes only loose objects, touches no ref and no worktree.

Verdict: **method reconstructed PARTIALLY.** The pass shape, the resolution
polarity, the cohort partition and operator rulings 1–5 are all settled in the
record. The **target ref**, the **workspace**, the **schema epoch** and the
contents of **Phase-0 cohort E** are not, and are routed to the operator in §8.

---

## 0. Corrections to the record, measured 2026-09-04

| Claim in the record | Source | Measured today | Status |
|---|---|---|---|
| platform branch is **UNPUSHED** | 8915, 8966, 9075, 9099 | `git rev-parse origin/feature/deferred-platform-recovery` = `a2176dfe225bd9d56d5eed642f7a8aff0a9485d2`, identical to the local branch | **FALSE NOW.** The branch is pushed and backed up on origin. The 9099 urgency ("only copy", "protect the branch") no longer applies to *this* branch. |
| "74 ahead, 405 behind" | brief §7 / §1 | `git rev-list --left-right --count release/0.8.0...feature/deferred-platform-recovery` = **496 / 74** | Behind-count is **496** at `cbae1ef0c` (405 at `f7d741d2f`, 408 at `77537c8de`). It grew 88 in one working day. |
| "405 commits, 758 files, 77 touched on both sides" | brief §7 | mainline side **843**, platform side **228**, **82 files touched on both sides** | Re-measure every time; the brief's figures were taken at `f7d741d2f`, and these at `cbae1ef0c`. |
| open decision 5 (`create_run` lock domain) is **open**, needs a PG two-connection proof | 8915 dec. 5; brief H9 repeats it as an outstanding caveat | **8949 records it DONE**: 4 PostgreSQL proofs pass in `tests/testcontainer/web/test_run_admission_custody_lock_postgres.py`; "The lock domains DO match (authority.mutate takes transaction_session_lock = the same advisory key)"; ADR-038 `FOR UPDATE` (obs `elspeth-obs-a346487a1f`) already proven both orders by `test_token_outcome_atomicity_postgres.py` — "no new test owed" | **CLOSED WITH EVIDENCE.** The brief and `elspeth-obs-a346487a1f` still carry it as open. Correct both. Caveat: `addopts` on **both** branches carries `-m "not … testcontainer …"`, so those proofs are **not in the default gate** — the proof exists, it is not a standing gate. |
| base pinned at `3575d0a72` (9075's ruling) | 9075 | `3575d0a72` and `b4405257a` are both now **ancestors of `release/0.8.0`** | Pin is stale, exactly as 9099 predicted. The pin must be re-taken at a live SHA. |
| `release/0.8.0` @ `91816d0f3` | this session's own brief | `git rev-parse release/0.8.0` = `cbae1ef0ca6e351a6e82b0afea53880b3c668d43` | The shared checkout moved **twice** while this document was being written: `91816d0f3 → 77537c8de → cbae1ef0c`. Any pin taken by hand is stale on arrival; capture `BASE=$(git rev-parse …)` inside the script. |
| 8949 `MERGEABILITY` line: "13 architecture adjudications" | 8949 | 8949's own enumeration is 9 session-facet + 7 landscape + 1 intentional-WIP = **17**; 8966 item (vi) also says **17** | Discrepancy **surfaced, not resolved**: 13 (8949 summary) vs 17 (8949 detail + 8966). Use 17 and re-derive. |

---

## 1. Pass shape — SETTLED

**A merge, onto the platform branch, with per-cohort hand resolution of the
conflicted set. Not a rebase.**

Evidence:

- 9075 **RULING**: *"merge onto pinned 3575d0a72 FIRST, before any further test
  burn. Merge cost is the only cost that grows monotonically; the ~212
  mechanical test fixes are flat and will not get harder if done second."*
- 8915 titles its own decomposition **"PER-COHORT RESOLUTION"** and records
  cohorts (a)–(g) with a stated rationale per cohort.
- 9099 re-measures with `git merge-tree` against merge candidates — a merge
  measurement, not a rebase one.
- Nothing in the nine comments proposes a rebase. 74 platform commits carrying a
  hand-resolved merge (`0b676d195`, H3) cannot be replayed commit-by-commit onto
  a moved base without re-litigating that hand resolution 74 times.

**Direction matters and the record contains both directions — do not confuse them.**
Comment 8915's `(c)+(d) … HEAD wholesale` describes the *previous* merge
`0b676d195`, which merged **lane → HEAD** (parents `7cd2fc6db` = HEAD,
`4c59c9d02` = lane). This pass is **mainline → platform**, target = platform.
Reuse 8915 for its **cohort partition**; take the **resolution polarity** from
9075 (§3 below). Transcribing "composer/* HEAD wholesale" into this pass would
instruct the resolver to drop the fences.

---

## 2. Preconditions (all before the first conflict is touched)

**P1. Fix H1 on mainline first — DISCHARGED.** Landed at `2a64b8ff7`
("fix(composer): repair state_revert surfacing only after hash verification"),
merged to `release/0.8.0` via `b74783a5e` and present in this baseline. The
`state_revert` durable surfacing now runs as `after_verified=`, so the 74
platform commits that *preserve* the write-after-verify invariant no longer
land on a base that *violates* it. **Carry one thing forward into M3:** the
new gate `tests/unit/architecture/test_guided_operation_replay_after_verified_sites.py`
pins the per-`(module, function, posture)` **count** of every
`reserve_or_replay_guided_operation` site (23 today, 8 entries). Merging 74
commits that touch `guided.py`, `guided_plan.py`, `guided_chat_atomic.py` and
`state.py` will move that census, and the gate is *supposed* to go red — its
inventory must be re-derived from the merged tree and each posture re-argued,
never widened to make the red go away.
*(Brief §7 action 3 / H1 — this precondition is brief-only, it is not in the nine comments.)*

**P2. Re-take the base pin at a live SHA, inside the script.** `3575d0a72` is
now an ancestor of everything. `release/0.8.0` moved `91816d0f3 → 77537c8de →
cbae1ef0c` during this session — twice, in one working day. `BASE=$(git rev-parse <target>)` — never a hand-expanded
short hash.

**P3. Settle the target ref with the operator** (§8 D1). The nine comments never
name `release/0.8.0`; 9099's *"the target is the FEATURE branch, not interim"*
was written when `feature/unified-lineage` was the live candidate.

**P4. Provision a dedicated worktree** for the pass and for every broad gate run
(brief §4 step 5). Arm A (fresh worktree, `test -z "$(readlink .venv)"`, then
`env -u VIRTUAL_ENV uv sync --python 3.13 --frozen --all-extras`) is required for
evidence-grade runs. Arm B (an existing `.claude/worktrees/*`) must **not** sync —
all seven symlink `.venv` to the main checkout — and needs
`PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src <repo>/.venv/bin/python -m pytest`
with **both** `elspeth.__file__` and `elspeth_lints.__file__` verified inside the
worktree. §8 D2.

**P5. Settle `SESSION_SCHEMA_EPOCH`** with the operator before resolving merge cohort M1.
Measured at `cbae1ef0c`: platform `48` (`models.py:248`), mainline **`50`**
(`models.py:267`, bumped by `b7992af66`). The collision is now **two** epochs
wide, not one, and it widened by an unrelated mainline fix — so it will widen
again if this is left to the end.
`src/elspeth/web/sessions/models.py` **is in the conflict set**, and the coupled
consumer `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py` is in the
both-sides set — it bakes the epoch into the rollback-baseline receipt string.
§8 D3.

**P6. Freeze the tree for the duration of any measurement.** Seven lanes share
this `.git`. Record `git rev-parse HEAD` before and after every long run; measure
corpus and suite on **frozen exports**, per 8949's own practice.

**P7. Do not touch the trust-tier allowlist or any `judge_metadata_signature`.**
Every checkpoint in the record states "signatures/allowlist untouched"; keep that
true. The +94 corpus burn is *owed*, not *masked* (§7).

---

## 3. The resolution rule — SETTLED (9075, hard boundary)

Two rules, quoted from comment 9075:

> **CONFLICT RULE (two rules, hard boundary): under `elspeth-lints/` resolve
> HEAD-first; under `src/elspeth/web/sessions/` and `src/elspeth/web/composer/`
> preserve LANE semantics — every resolution must keep the
> `session_operation_lease` / operation-context threading, because HEAD's side is
> the unfenced side. Dropped fences do not fail loudly: `81f140501` manifested as
> a hang (leaked 300 s lease, joiners polled to expiry).**

### 3.1 The failure signature a resolver must recognise

A dropped fence does **not** present as a red test. From 8917, measured not
inferred:

> the fork route's `fork_session(fence, …)` raised `TypeError` (lane requires
> `SessionForkParentAuthority`); its `except` called `fail_guided_operation`
> without `session_operation_context` → second `TypeError` → 500; the guided row
> stayed `in_progress` with a 300 s lease and the parent session lease leaked;
> every replay joiner then polled `reserve_or_replay_guided_operation`
> (`guided_operations.py:590`, `_POLL_SECONDS=0.05`) until DB-clock expiry.
> Suite-wide that is minutes per affected test.

So: **a scoped green run does not clear this cohort.** The tells are a hung
suite, a `TypeError: missing … session_operation_context`, or a guided row left
non-terminal. 8919 records the same class again at the *settled*-authority seam
(`81f140501`), and 8917 records a third at the planner entry (`ebdd04a52`: every
guided planner entry 500'd as `TypeError → operation_failed`).

### 3.2 The lesson 9075 wrote for the next agent

> **LESSON FOR THE NEXT AGENT: a file-level restore is not a safe conflict
> resolution. It silently reverted a security fix.**

`54ce1f9cf` restored the `elspeth-lints` tree to `7cd2fc6db` wholesale and thereby
reverted **this lane's own path-containment fix while keeping its tests** — a
genuine reintroduced path traversal (`bundle_id` crosses the MCP trust boundary
into a `staged_dir` path). Fixed in `a2176dfe2`. Therefore the "HEAD-first" arm of
the rule is **HEAD-first per hunk, with a lane-fix inventory**, never
`git checkout <ref> -- <path>`.

### 3.3 H3 — the three hand-resolved files, and why they need a re-audit and not a diff

H3's claim is **VERIFIED, and it is stronger than the brief states**
(`git rev-parse 0b676d195^:<p> 0b676d195^2:<p> 0b676d195:<p>`; parents:
`^` = `7cd2fc6db` unified-lineage, `^2` = `4c59c9d02` lane):

| path | parent1 blob | parent2 blob | merged blob | verdict |
|---|---|---|---|---|
| `src/elspeth/web/sessions/service.py` | `d06359bca` | `48230374c` | `d8060a3f9` | **matches neither** |
| `src/elspeth/web/sessions/protocol.py` | `b60b722fb` | `9e74e4955` | `7380b41db` | **matches neither** |
| `src/elspeth/web/sessions/pending_interpretation.py` | *path does not exist in `0b676d195^`* | `b4a313069` | `585394229` | **matches neither** (lane-new file, then hand-edited) |

Now join that against **this** pass's measured conflict set:

- `sessions/service.py` — **conflicts.** The resolver will see it.
- `sessions/protocol.py` — **auto-merges.** In the both-sides set, `Auto-merging`
  in the merge-tree messages, absent from the conflict list.
- `sessions/pending_interpretation.py` — **in neither list.** Mainline has not
  touched it since `7cd2fc6db` (`grep -x` against the mainline-side diff: no match).

**Therefore: for two of the three hand-resolved files, git will lay mainline's
changes onto a hand-resolved blob and surface nothing at all.** That is the
measured reason H3 needs a standalone re-audit step rather than a diff-driven
resolution.

**H3 RE-AUDIT STEP (mandatory; merge cohorts M1 and M2 both trigger it):** before
accepting any auto-merge or resolution in those three files, re-derive from the
merged tree — not from either parent's diff — that (i) every writer still demands
an exact `SessionOperationContext`, (ii) the protocol declaration and the impl
signature agree, and (iii) no lane fence was silently widened by a mainline hunk.
`8915`'s (b) records what the hand resolution actually decided in `service.py`
(symbol-level 3-way: lane base for 115 lane-only symbols, HEAD for 16 HEAD-new,
36 both-changed hand-merged) — that inventory is the audit's checklist, not its
substitute.

---

## 4. Cohort decomposition and order

**Two distinct cohort systems exist in the record. Do not merge them into one list.**

- **(a)–(g)** in comment 8915 — the **merge-resolution** cohorts. This is the
  only recorded decomposition of the merge itself and it carries a rationale per
  cohort. Reused below as the partition; polarity re-derived from §3.
  **Relabelled `M1`–`M6` throughout this document** so the letters cannot be
  confused with the Phase-0 cohorts; each row states the 8915 letter it descends
  from.
- **A/B/C/D/E** in comments 8949 / 8966 — the **Phase-0 remediation** cohorts
  (B = ruling-5 PG proofs, C = contention rewrite + characterization leasing,
  D = architecture repin). They belong to the completion criterion (§7), not to
  the merge order. **Cohort E is never defined anywhere in the nine comments** —
  8966 says "cohorts A/B/D and part of C/E" and nowhere states E's contents.
  **UNSETTLED**; do not infer it (§8 D6).

### 4.1 Measured conflict set — REBASELINED at `cbae1ef0c`

`git merge-tree --write-tree --name-only --messages release/0.8.0 feature/deferred-platform-recovery`
→ exit 1, **33 conflicted files**, all content conflicts (no file/directory
conflict today — 9075's `.claude/skills/loomweave-workflow` file/dir conflict has
resolved itself).

Conflict-count growth, and why the target choice moves it:

| candidate target | conflicted files | behind / ahead |
|---|---|---|
| `release/0.8.0` @ `cbae1ef0c` | **33** | 496 / 74 |
| `feature/unified-lineage` @ `0e11d0580` | **28** | 351 / 74 |
| `interim-merge-target` @ `4b34d972c` | **24** | 325 / 74 |

*(9075 measured 12 + 1 against `3575d0a72` on 08-31; 9099 measured 19 vs
`feature/unified-lineage` and 24 vs `interim-merge-target` on 09-01; this
document measured 31 vs `release/0.8.0` @ `77537c8de` earlier on 09-04. The
trend 9075 predicted — "merge cost is the only cost that grows monotonically" —
holds without exception: 12 → 19 → 28 → 31 → 33 on the same lineage, the last
step costing two files in a single working day. **Nothing has ever left the
conflict set.** The two non-`release/0.8.0` rows are unchanged only because
those refs have not moved.)*

**The two files the rebaseline added, and why they are not routine.** Both
arrived with the 2026-09-04 baseline (`51b43a770`, then `cbae1ef0c`):

- **`src/elspeth/web/sessions/routes/composer/proposals.py`** — entirely new to
  the set. The `strany/tool-result-envelope` merge message records that this
  same file carried *"the semantic merge conflict … that git auto-merged
  silently and only the mypy hook caught."* That is §3.1's failure signature,
  already realised once on this exact path. Resolve it under §3.3's re-audit
  rule, not by reading the diff.
- **`src/elspeth/web/sessions/routes/composer/guided_plan.py`** — promoted from
  the "auto-merging, still audit" column of M3 into a real conflict. It holds
  five `reserve_or_replay_guided_operation` sites, so a careless resolution is
  a fence-loss candidate, not a text merge.

Both belong to **M3**, which keeps M3 the cohort where the resolution rule
earns its keep.

**9099 also rules the target out of one of these:** *"interim-merge-target is a
BUG-FIX-ONLY interim branch and this multi-replica platform program must not
land on it"* — John's scoping. So `interim-merge-target`'s 24 is the cheapest and
the **forbidden** option. §8 D1.

### 4.2 Cohort order — merge cohorts

Order is **dependency order, cheapest-signal-first within a tier**: the schema
and contract layer must be settled before anything that threads a context through
it, and tests are resolved after the production surfaces they exercise. This is
the order 8915 recorded and 8917/8919 executed.

| # | cohort | files in today's conflict set | rationale (8915) | polarity for THIS pass |
|---|---|---|---|---|
| **M1** | **Foundation / schema / contracts** *(from 8915(a))* | `src/elspeth/web/sessions/models.py`; (auto-merging, still audit) `sessions/schema.py`, `contracts/blobs.py`, `web/_aws_ecs_acceptance/receipt_contracts.py`, `config/cicd/contracts-whitelist.yaml` | 8915(a): coordination modules, `contracts/session_operation.py`, `models.py` lane authority tables, epoch, protocol union | **LANE semantics.** The lane authority tables and `session_operation_fences` exist only here (5 platform src files, 0 mainline). **Blocked on epoch ruling D3.** |
| **M2** | **`sessions/service.py` + `sessions/protocol.py` + `pending_interpretation.py`** *(from 8915(b); `protocol.py` **moved here from 8915(a)** — a deliberate deviation from 8915's partition, so all three H3 files are audited together)* | `sessions/service.py` (conflicts); `sessions/protocol.py` (auto-merges); `pending_interpretation.py` (untouched by mainline) | 8915(b): symbol-level 3-way, 115 lane-only / 16 HEAD-new / 36 both-changed | **LANE semantics + mandatory H3 re-audit (§3.3).** Symbol-level 3-way, never file-level. Two of three surface nothing to git. |
| **M3** | **Composer + session routes (the threading surface)** *(from 8915(c)+(d))* | `web/app.py`, `composer/protocol.py`, `composer/turn_audit.py`, `composer/guided/emitters.py`, `routes/_helpers.py`, `routes/messages.py`, `routes/sessions.py`, `routes/composer/{compose,guided,guided_chat_atomic,guided_plan,proposals,state}.py`; (auto-merging, still audit) `composer/service.py`, `composer/tool_batch.py`, `composer/tools/blobs.py`, `routes/interpretation.py`, `core/blobs_inline.py`, `web/blobs/service.py`, `web/execution/service.py` | 8915(c)+(d) took these **HEAD wholesale in the OTHER direction**; 8917 then had to re-thread all of them | **LANE semantics — this is the cohort 9075's rule was written for.** Every resolution keeps `session_operation_lease` / context threading. This is where a dropped fence becomes a hang (§3.1). **Note:** `web/preferences/service.py` is in **neither** the conflict set nor the both-sides set (untouched by either side since `7cd2fc6db`), so ruling 3 is a **standing semantics constraint** on this cohort — nothing may reintroduce compose-lease serialization of preferences — not a conflict to resolve. |
| **M4** | **`elspeth-lints/`** *(from 8915(e))* | (auto-merging) `elspeth-lints/src/elspeth_lints/core/review_bundle.py` | 8915(e): HEAD | **HEAD-first *per hunk*.** Never a file-level restore — that is exactly what `54ce1f9cf` did and it reverted a path-traversal fix (§3.2). Inventory the lane's containment fix (`resolve_staged_bundle_path`, `_require_str_arg` str-subclass rejection, `stage_scan`/`stage_rekey` invalid-`bundle_id` rejection, all landed in `a2176dfe2`) and confirm each survives. 8915(e) also names `src/elspeth/contracts/runtime_val_manifest.py`: it is in **neither** set today (platform-side only, untouched by mainline) — nothing to resolve, do not hunt for it. |
| **M5** | **Tests** *(from 8915(f))* | 14 conflicted test files: `tests/e2e/recovery/test_sink_effect_deployment_profiles.py`, `tests/integration/web/test_preflight_per_class.py`, `tests/unit/web/composer/guided/test_emitters.py`, `.../test_compose_loop_interpretation_review_dispatch.py`, `.../test_request_interpretation_review_tool.py`, `tests/unit/web/sessions/routes/composer/test_state_boundaries.py`, `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`, `.../test_guided_operation_fork_service.py`, `.../test_guided_operations_service.py`, `.../test_guided_start.py`, `.../test_interpretation_events_routes.py`, `.../test_interpretation_events_service.py`, `.../test_interpretation_events_table.py`, `.../test_routes.py` | 8915(f): HEAD's conflicting tests from HEAD, lane's new test files kept | **Mixed, per file.** Mainline's *new coverage* is kept; the lane's *fence contracts* are kept. Where they collide the fenced contract wins and mainline's assertion is re-expressed against a leased harness (`tests/helpers/session_fences.py`, `tests/helpers/composer_lease.py`). **Preserve the platform-only guard test** `tests/unit/web/sessions/routes/test_guided_operations.py::test_terminal_replay_never_acquires_session_authority` — measured: **1 hit on `a2176dfe2`, 0 on `release/0.8.0`** (`git grep -c … -- tests`, zero-count files are omitted by `-c`, hence the explicit 0). `git log -S` shows it was never on mainline; there is nothing to fall back on. |
| **M6** | **Docs / config / runbooks** *(from 8915(g))* | `README.md`, `docs/guides/sharing-pipelines.md`, `docs/runbooks/aws-ecs-deployment.md`, `docs/runbooks/staging-session-db-recreation.md` | 8915(g): HEAD | **HEAD (mainline).** Plus H4: delete or supersede `a2176dfe2:docs/superpowers/plans/2026-08-02-multi-replica-platform-pause-handoff.md` (the stale 369-line 23:09 revision, missing the 23:12 hash-drift paragraph) and retire the branch's whole `docs/superpowers/` tree — 5 files, measured — which mainline renamed at `8548a5e29`. A merge otherwise resurrects retired paths. |

M4 and M6 carry no fence risk and can be resolved in parallel with M1–M3.
M2 **must not** start before M1; M3 **must not** start before M2; M5 is resolved
last because its resolutions are re-expressions of M3's decisions.

The 33 conflicted files partition exactly across M1–M6 with no overlap and no
remainder: M1 = 1, M2 = 1, M3 = **13**, M4 = 0, M5 = 14, M6 = 4. Both files the
2026-09-04 rebaseline added landed in M3, so the cohort that already carried the
most fence risk is the one that grew.

### 4.3 The mechanical cohort survives the merge — re-verified today

9075 verified the ~212-failure mechanical cohort
(`tests/unit/web/execution/{test_service,test_routes,test_session_operation_lease}.py`)
conflict-free **against `3575d0a72`**, which is now stale. Re-measured against
`release/0.8.0`: none of the three appears in the 31-file conflict set, and none
is touched by mainline at all since the merge base;
`test_session_operation_lease.py` does not exist on mainline. **9075's ruling
holds: merge first, burn those 212 second.** They are flat cost.

---

## 5. Operator rulings 1–5 — SETTLED (comment 8925)

All five come from one comment. John's framing, quoted:

> **"agreed to all, including dropping the table; we are still in our
> no-tech-debt window but it's closing, so use it."** — comment 8925

| # | ruling, quoted from comment 8925 | comment id | state today |
|---|---|---|---|
| **1** | *"Blob custody: RE-DERIVE the lane's authority-routed ledger on HEAD's blob layer as Task 9 in Phase 1; do not restore lane files wholesale. Interim nullable ledger columns stay with a schema-epoch note."* | **8925** (decision raised 8915 dec. 1, refined 8917) | Deferred to Phase 1 Task 9. During this pass: **do not** restore the lane's `BlobReplacementCoordinator` / deletion-plan / fork-write-fence / blob-effect-receipt files; keep `blob_deletion_cleanups` ledger columns NULLABLE so mainline's blob writer inserts. |
| **2** | *"Progress registry: DEFER to Task 8; DROP the unused `composer_progress_snapshots` table now (epoch 48 has not shipped, so no second epoch bump)."* | **8925** (raised 8915 dec. 2) | **Executed** at `75bd20ca1` (8949: "composer-progress facet + both tables dropped, epoch 48 unchanged with note"). Mainline's in-memory registry is the one to keep through the merge. |
| **3** | *"Preferences/user-secret: HEAD + stage-3 refinement is FINAL — preferences serialized by the per-session write lock, never by the compose lease (mid-compose trust downgrade must always land); user_store row versions kept. Record as a deliberate Task-5 inventory exemption."* | **8925** (raised 8915 dec. 3, refined 8919 `eb987d543`) | **FINAL.** In this pass, `preferences/service.py` resolves **mainline-side** — the one documented exception to §3's "preserve LANE semantics" inside the sessions tree, and it is an exception *by ruling*. The Task-5 inventory exemption must be recorded, and 8949 notes the old `RepositoryUserPreferenceAuthority.apply_patch` reviewed entries are now **dead**. |
| **4** | *"Fenced-by-analogy writers: ACCEPT all four (`add_messages_atomic`, replay-repair surfacing callback with its own short COMPOSE lease, `request_interpretation_review` → `create_pending_interpretation_event`, `_execute_update_blob` retention binding); add one regression proving the replay-repair is idempotent under a lost lease."* | **8925** (raised 8917 "FENCED BY ANALOGY — REVIEW") | **Executed**; idempotency regression incl. the lost-lease arm landed at `75bd20ca1` (8949). All four fences must survive this merge — they are precisely the "HEAD's side is the unfenced side" cases. |
| **5** | *"`create_run` lock domain: NOT optional — write the `ELSPETH_TEST_POSTGRES_URL`-gated two-connection barrier proof (update-vs-run and delete-vs-run, exactly one proceeds) in Phase 0 before merge; same shape covers obs `elspeth-obs-a346487a1f`'s ADR-038 FOR UPDATE debt. If it fails it is a P0 on the current AWS path and ruling 1 moves to 'now'."* | **8925** (raised 8915 dec. 5) | **DONE AND PASSING** (8949, Phase-0 cohort B): 4 PG proofs in `tests/testcontainer/web/test_run_admission_custody_lock_postgres.py` — bare custody-lock exclusion; `delete_blob` parked after its guard with run admission proven a real pg lock waiter via `pg_stat_activity`; the same through the composer's `update_blob`; mirror order refused by both guards. *"The lock domains DO match."* ADR-038: no new test owed. **Ruling 1 therefore does NOT move to "now".** See §0 — the brief and the obs still record this as open. |

### 5.1 Standing rejections carried by the same record

- **Do not merge `recovery/deferred-platform-wip-broken` (`3f3857d20`)** — 8966
  "reference-only, never merge". Harvest exactly three things per brief §4 step 8.
- **Do not restore or extend the general Python object-graph scanner.** `c4ff80553`
  retired it; the live pin is `tests/unit/web/sessions/test_operation_fence_wiring.py:234`.
- **Do not copy architecture-inventory counts forward.** 8919: *"the pause handoff
  itself forbids copying counts forward"* — re-run the authoritative inventory and
  adjudicate the `WriterIdent` deltas; a blind repin is not a resolution.

---

## 6. The 536-failure classification — comment 8966

Measured at the pre-lints tip on a **frozen export**, 49 m 40 s wall:
**536 failed / 44,629 passed / 67 skipped / 4 errors**
(`scratchpad/recover/fullsuite-closeout.log` — that log is **GONE**, see §6.2).

**Worker count: UNSTATED in 8966, but the run was parallel.** The platform
branch's `addopts` carries no `-n`, yet 44,629 tests passed in 49 m 40 s;
mainline's own `addopts` comment measures the serial suite at **~17 hours**
(~42 tests/min). An explicit `-n` was therefore passed on the command line — the
lane was doing this throughout (`-n 4` in 8917 and 8919, `-n 2` in 8949). The
**exact worker count is UNSETTLED**, which is one more reason the number is not a
comparison target.

| class | count | what it was | what it implied |
|---|---|---|---|
| (i) frozen-export artifacts | ~44 | ignore-policy tests need a `.git`; **PASS in the worktree** | Not defects. Exclude with reason, or export with a thin `.git`. A methodology artifact of the freeze, not of the merge. |
| (ii) lints | ~105 | whole-tree lint gates | **FIXED** by `54ce1f9cf` → 25 residual → **10** after `a2176dfe2`'s containment fix (9075). The residual 10 are whole-tree gates flagging lane src = corpus-burn cohort work. |
| (iii) execution unit | ~212 | `test_service` 170, `test_routes` 29, `test_session_operation_lease` 13: direct `_run_pipeline()` calls missing the required kw-only `session_operation_lease` | **Mechanical, flat cost, no production bypasses.** Apply the `tests/helpers/composer_lease.py` recipe. 9075 verified conflict-free; re-verified today (§4.3). This is the single largest class and it is *not* a design problem. |
| (iv) guided integration | ~70 | `wrong_stage_intent` 19, `respond` 17, `chat_schema8_atomic` 11, `guided_full` 8, `arbitrary_dag_review` 7, `respond_schema8` 4 | *"likely the leased-loop recipe but **verify individually**; `test_respond` guards the three replay defects, treat replay-semantics diffs as real."* The one class where a mechanical fix would destroy real coverage. |
| (v) blob / clock contracts | ~35 | `test_web_blob_fencing` 21, `test_blobs_inline` 9, `test_database_clock_authority` 5 | *"probable follow-on from `425924a67` envelope/lock-domain, **diagnose properly**."* Not mechanical. |
| (vi) architecture adjudications | **17** | 9 session-facet + 7 landscape-vs-engine drift + 1 intentional WIP | **Judgment, not repin.** Each needs a fencing adjudication. The global WIP inventory test stays intentionally red until Task 5 completes (lane convention). *(8949's summary line says 13; its own detail and 8966 say 17 — §0.)* |
| (vii) misc | ~24 | `test_config` 4, `audit_readiness` 7, property 6, docs 4, website 2, plus 4× asyncio `"Task exception was never retrieved"` | The asyncio four need their source found, not their symptom silenced. |

Sum of the stated classes ≈ 507 of 536. Two reasons the arithmetic is loose and
neither is a defect in the classification: the remainder is simply unallocated in
the record, and row (vi)'s **17 is an adjudication count, not a failure count**
(8949 records "24 architecture failures → 14" after the attributable repin).
Do not treat 507 or 17 as measurements.

### 6.1 The classification is HISTORY, not a baseline — 9075's ruling

> *"the 536-failure classification in 8966 was measured against `7cd2fc6db` and
> becomes historical context, NOT a comparison target, the moment the merge lands.
> Re-baseline with a fresh full-suite run. **Do not diff against 8966's numbers —
> that is how a merge-induced regression gets filed as 'pre-existing, already
> classified'.**"*

This is the same conclusion H10 reaches by a different route (branch `addopts`
differ), and it binds harder: even a like-for-like worker count would not make the
two runs comparable, because the tree changed underneath.

### 6.2 The evidence is gone

9099: the worktree `.claude/worktrees/deferred-platform` was removed in the
2026-09-01 cleanup and its gitignored `.scratch/deferred-platform-handoff/` went
with it — `HANDOFF-PROMPT.md`, `fullsuite-closeout.log`, `status-report.html`,
`corpus-base7cd2.log`, `corpus-closeout-frozen.log`, `lints-after-containment.log`,
`lints-remaining.txt`. **The branch was explicitly protected and is intact at
`a2176dfe2`** (and, contrary to all four comments, is now on origin — §0).
The substance survives in 8966 and 9075; only the raw logs and the takeover prompt
are lost, and 9075 already required re-baselining them.

---

## 7. Completion criterion — how you know the pass is done

**Not a test-count delta.** H10 forbids it (mainline `addopts` ends `"-n", "12"`;
the platform branch's `addopts` has no `-n` at all — verified today by reading
both `pyproject.toml:450-455`, so a bare `pytest tests/` on the platform branch is
the ~17-hour serial run and `-n 0` is a no-op rather than an override). 9075
forbids it independently (§6.1). The criterion is composite and every part is
recorded; it is 8949's `MERGEABILITY: NO` blocker list, updated by 9075.

The pass is complete when **all six** hold:

1. **Both 9075 conflict rules held, per cohort, on the record.** For every file in
   §4.2 merge cohorts M1/M2/M3/M5, a stated resolution and the rule it followed. Plus the H3
   re-audit (§3.3) signed off for all three files — including the two git never
   surfaces.

2. **A fresh full-suite run on a frozen export of the merged tip**, `-n 12` passed
   **explicitly** and **stated beside the count**, HEAD recorded before and after.
   Re-baselined, **not diffed** against 536. Every remaining failure classified in
   the new run's own terms. Flaky-set discipline: re-run any red in
   `tests/e2e/recovery`, `tests/integration/pipeline`, `tests/unit/engine/orchestrator`
   with `-n 0` before attributing it to the integration (`elspeth-0077cb7789`, open;
   H10 notes the flaky set overlaps this program's subject matter exactly).

3. **No hang and no leaked lease.** The §3.1 signature is absent: no suite hang, no
   `TypeError: missing … session_operation_context`, no guided row left
   non-terminal. This is a *separate* check from (2) because a dropped fence can
   present as a slow green.

4. **The 17 architecture adjudications resolved by judgment**, not repin — 9
   session-facet (incl. ruling 3's preferences contract rewrite and the 4 reviewed
   entries with no live twin), 7 landscape-vs-engine drift, 1 intentional global-WIP
   red which **stays red** until Task 5 completes.

5. **Trust-tier corpus measured with ONE regex on frozen exports of both trees**,
   `R_TB_SUPPRESSED` excluded, `^path:line:col: R` — the discipline that resolved
   8916/8917's 1794-vs-1411 discrepancy as a regex artifact. Baseline from 8949:
   `7cd2fc6db` = **1304 net**, closeout tip = **1398 net**, **+94 owed**. The burn
   is a follow-up cohort, per-file, blanket-migration doctrine; it is **not masked**
   and **no allowlist or signature is edited** to reach it.

6. **mypy at the HEAD-inherited baseline, not zero** — `operator_telemetry.py:557`
   and `emitters.py:321` are pre-existing. Ruff check/format clean. `check-contracts`
   un-skipped and green (8949: all 8 lane `dict[str, Any]` sites now exact types).

Phase-0 cohorts **A, B, D** are closed and **C** substantially so (8966); **E is
undefined in the record** (§8 D6). Their closure is an input to this criterion,
not a substitute for it.

---

## 8. What remains an OPERATOR DECISION

| # | decision | why it cannot be taken by an agent | measured input |
|---|---|---|---|
| **D1** | **Which ref is "mainline" for this pass** — `release/0.8.0`, `feature/unified-lineage`, or something else. | The nine comments never name `release/0.8.0`; 9099's *"target is the FEATURE branch"* was written against a now-ancestor SHA. Conflict count, epoch decision and cohort contents all move with the choice. `interim-merge-target` is **ruled out** by John (9099: bug-fix-only interim). | **33** conflicts vs `release/0.8.0` @ `cbae1ef0c`; 28 vs `feature/unified-lineage` @ `0e11d0580`; 24 vs `interim-merge-target` (forbidden). Behind-counts 496, 351, 325; ahead 74 in all three. **The `release/0.8.0` row moves every few hours and the others do not, which is itself an argument for deciding D1 now rather than re-measuring it.** |
| **D2** | **Which checkout or worktree hosts the pass.** | The platform branch has no worktree (9099: removed in the 09-01 cleanup). Brief §4 step 5's two arms are written for *test* runs, not for a 74-commit merge; Arm B's shared `.venv` is unsafe for anything that installs. Seven lanes share this `.git`. | All seven `.claude/worktrees/*` symlink `.venv` → main checkout. `<repo>/.worktrees/` does not exist. |
| **D3** | **`SESSION_SCHEMA_EPOCH`: 48, 50, or 51.** *(Was "48, 49 or 50" — mainline has since moved to 50, so the option set shifted up by one.)* | H6. Platform 48 (post-ruling-2 table drop, "epoch 48 has not shipped"); mainline **50**, set by `b7992af66` (the guided-decline-rebind fix) — the second unrelated bump in a week, so the collision widened from one epoch to two while this document was being written. `models.py` conflicts; `receipt_contracts.py` bakes the value into a rollback-baseline receipt string. **The drift itself is the argument for settling D3 early: every mainline bump re-opens it.** | `a2176dfe2:models.py:248` = 48; `release/0.8.0:models.py:267` = **50**. Both files in the both-sides set. |
| **D4** | **Which deferred-platform plan governs** — restore the 27-task revision (blob `1e62a12cc`) to `docs/plans/`, or formally supersede it. | Brief §7 action 2. Nothing downstream is safely numberable: the same task number names different work in the two revisions, and one puts provider packaging inside the "stop before Task 14" boundary. Compounded by H8 (branch carries the v3 spec against the v1 plan; zero `- [x]` on both revisions). | — |
| **D5** | **`state_envelope.py` cherry-pick to mainline: now, with the merge, or not at all.** | 9075 required re-verifying the defect reproduces before cherry-picking (`61e561061`, `7f7a2ec75` touched `composition_states`); 9099 records it **not re-verified**. H7 warns the naive fix (`_unwrap_envelope` alone) downgrades a fail-**closed** prong to a fail-**open** one — a blob referenced only from a source's options would read as unreferenced. `elspeth-3db5745ba7` is open P1; **do not file a duplicate**. | `src/elspeth/web/sessions/state_envelope.py`: **PRESENT** on `a2176dfe2`; **ABSENT** on `release/0.8.0`, `feature/unified-lineage`, `interim-merge-target`. The writer/reader asymmetry is live on mainline today. |
| **D6** | **What Phase-0 cohort E was.** | 8966 claims "part of C/E" complete; **E is defined nowhere in the nine comments.** Either the operator knows, or it is re-derived from the remaining 8949/8966 open items and re-named. | Cohorts A (stage 1–3 merge), B (ruling-5 PG proofs), C (contention + characterization leasing), D (architecture repin) are all defined. E is not. |
| **D7** | **Whether the 3 mainline context-free call sites get a lease wrapper or a written exemption.** | §4 step 7. Mainline's `save_composition_state_with_interpretations` (`370e3bdf0`) has no fencing concept at all; wrapping it is a semantic change to mainline behaviour, and exempting it puts an unfenced audit-primary writer inside a fenced tree. | See §9. |
| **D8** | **Correcting the record.** Ruling 5 is closed with evidence (§0) but the brief (H9) and `elspeth-obs-a346487a1f` still read as open; the platform-branch `AGENTS.md` is 107 lines shorter than mainline's (H10) and a resumer reading it gets no `-n 12` guidance, no xdist flaky-set gotcha and no whole-tree-gates STOP block. | Both are tracker/doc writes with program-wide reach. | — |

---

## 9. The `SessionOperationContext` threading budget (brief §4 step 7) — VERIFIED

Both figures in the brief are confirmed today.

**Platform branch `a2176dfe2`, `src/` only: 276 hits across 23 files**
(`git grep -c 'SessionOperationContext' a2176dfe2 -- 'src/*'`):

```
sessions/service.py 71   sessions/protocol.py 60   coordination/repository.py 38
composer/service.py 28   coordination/lifecycle.py 21   sessions/routes/_helpers.py 6
shareable_reviews/service.py 5   composer/protocol.py 5   sessions/routes/composer/state.py 4
sessions/routes/composer/guided.py 4   composer/tool_batch.py 4
sessions/routes/composer/pipeline_settlement.py 3   execution/service.py 3
execution/protocol.py 3   composer/turn_audit.py 3   audit_readiness/service.py 3
core/blobs_inline.py 3   contracts/session_operation.py 3   sessions/routes/sessions.py 2
sessions/routes/guided_operations.py 2   sessions/_auto_title.py 2
coordination/__init__.py 2   coordination/contracts.py 1
```
(23 files listed; sum = 276.)

**Mainline `release/0.8.0`, `src/` only: 0 files, 0 hits.**
`session_operation_fences`: **5 platform src files** (`coordination/repository.py`,
`coordination/run_recovery_authority.py`, `sessions/models.py`, `sessions/schema.py`,
`sessions/service.py`), **0 mainline files**.

### 9.1 The budget: mainline's context-free write path

> **"Expect a type error, not a merge conflict — git will not flag this."** (brief §4 step 7)

Mainline grew `save_composition_state_with_interpretations` (`370e3bdf0`,
2026-09-01) after the merge base. Sites needing a `SessionOperationLease.acquire`
wrapper or an explicit exemption ruling (D7):

- defined `src/elspeth/web/sessions/service.py:9732` (calls
  `_prepare_or_create_pending_interpretation_event` at `:9755`)
- declared `src/elspeth/web/sessions/protocol.py:3245`
- called `src/elspeth/web/sessions/routes/composer/state.py:786` and `:916`
- repair-surfacer call `state.py:642-646` (surfacer defined `state.py:91-116`) —
  **this is also H1**, so P1 and D7 land on the same lines.

The acquisition pattern to apply, recorded verbatim in 8915:

> routes acquire `SessionOperationLease.acquire(service.session_operation_authority,
> session_id=…, operation_kind=COMPOSE|PROPOSAL|EXECUTE|BLOB_READ|SESSION_FORK,
> owner_instance_id=service.session_operation_owner_instance_id,
> lease_seconds=service.session_operation_lease_seconds)` under the compose lock and
> pass `lease.context`; guided routes use `reserved.session_operation_context`;
> `ComposerServiceImpl.compose` / `ToolBatchContext` / `_persist_pipeline_planner_audit`
> / `surface_pending_interpretation_reviews` / `persist_turn_audit` take the context;
> `ExecutionService.execute` takes `session_operation_lease`.

8915 also notes these are **per-file independent and can be parallel lanes** —
the one part of this pass that parallelises safely, because mypy is the oracle.
8917 measured that oracle working: `mypy src/elspeth/web` **101 → 2** errors after
the threading pass, where 2 == the HEAD baseline.

**Budget after the merge:** the 276/23 must not shrink, and the delta is
`0 → N` new sites on mainline's side. Measure it as
`mypy src/elspeth/web` on the merged tree; the target is the **HEAD-inherited
baseline (2)**, not zero (§7 item 6). Every "Missing named argument
session_operation_context" is a site the merge created and a hang it prevented.

### 9.2 Preserve the guard test

`tests/unit/web/sessions/routes/test_guided_operations.py::test_terminal_replay_never_acquires_session_authority`
— **1 hit on `a2176dfe2`, 0 on `release/0.8.0`** (measured; `git grep -c` omits
zero-count files, so the 0 is stated explicitly). `git log -S` per the brief shows
it was never added to mainline. There is nothing to fall back on if a resolution
drops it.

---

## 10. Measurement appendix

All commands run from the repository root on `release/0.8.0`, read-only. The
raw measurement files lived in session scratch and are not retained; every
figure below is reproducible by re-running the commands as written.

```
git rev-parse origin/feature/deferred-platform-recovery
  → a2176dfe225bd9d56d5eed642f7a8aff0a9485d2   (== local branch; PUSHED)
git rev-parse release/0.8.0
  → cbae1ef0ca6e351a6e82b0afea53880b3c668d43
  (91816d0f3 → 77537c8de → cbae1ef0c across one working day; == origin, PUSHED)
git merge-base release/0.8.0 feature/deferred-platform-recovery
  → 7cd2fc6db08714386bfb7e9d1ddd9b012f8c589d
git rev-list --left-right --count release/0.8.0...feature/deferred-platform-recovery
  → 496   74                                        (was 408 74 at 77537c8de)
git merge-tree --write-tree --name-only --messages release/0.8.0 feature/deferred-platform-recovery
  → exit 1, 33 conflicted files, all content conflicts   (was 31 at 77537c8de;
    added routes/composer/proposals.py and routes/composer/guided_plan.py)
git diff --name-only 7cd2fc6db release/0.8.0                        → 843 files
git diff --name-only 7cd2fc6db feature/deferred-platform-recovery   → 228 files
comm -12                                                            →  82 files both sides
git rev-parse 0b676d195^ 0b676d195^2   → 7cd2fc6db… / 4c59c9d02…
  (H3: all three named files match neither parent — table in §3.3)
git grep -c 'SessionOperationContext' a2176dfe2 -- 'src/*'   → 23 files, 276 hits
git grep -c 'SessionOperationContext' release/0.8.0 -- 'src/*' → 0 files
git show a2176dfe2:pyproject.toml     | sed -n '450,455p'   → addopts, no -n
git show release/0.8.0:pyproject.toml | sed -n '450,462p'   → addopts … "-n","12"
git show a2176dfe2:…/models.py     | grep SESSION_SCHEMA_EPOCH → 248: = 48
git show release/0.8.0:…/models.py | grep SESSION_SCHEMA_EPOCH → 267: = 50
  (was 255: = 49 at 77537c8de; bumped by b7992af66 — the collision is now 2)
git cat-file -e <ref>:src/elspeth/web/sessions/state_envelope.py
  → PRESENT a2176dfe2; ABSENT release/0.8.0, feature/unified-lineage, interim-merge-target
```

No test suite was run for this document; no count in it is a test result of mine.
The 536 classification, the corpus numbers and the mypy 101→2 figure are all
quoted from the tracker with their comment ids, not re-measured.
