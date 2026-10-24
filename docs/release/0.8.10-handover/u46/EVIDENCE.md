# U-46 fix: per-step evidence (branch codex/0.8.10-u46-fix, from 582cc93c2)

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, and THIS LANE (u45-firstcompose) OWNS IT:** U-46 (b) rebase+land, then (a) rev 3 + implementation + Mythos pass, then reserve-contract tests bounded; probably also the suite-wide pytest timeout (the coordinator confirms at resume). See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. Maintainer's order, 2026-10-25.

> **Coordinator corrections (10-25, from COLD-START-REVIEW.md). These override the text below:**
> - S3: the P1 equivalence evidence (`corpus_old.json`/`corpus_p1.json`, 04:46-04:49) was produced by the GITIGNORED lane copies of `corpus_compare.py` and `u46oracle.py` in this dir, not by the later tracked copies on `codex/0.8.10-u46-diagnosis` (`6af0328e3`, which differ by 64 and 7 lines). Both versions are copied to `docs/release/0.8.10-handover/instruments/u46/`.
> - I3: the `timeout 7200` for the fencing-tool run is GRANTED by the coordinator for Task 0.2/0.3 (a single process, one at a time).


Logs are in `.claude/lanes/release-0.8.10/u46fix/out/`.

## T1: the tool's exit status gates. Commit a548991c6.

- `tests/unit/scripts/test_fencing_inventory_verdict.py` passes: `10 passed` (`t1_tests3.log`, exit 0).
- **Mutation control.** Forcing the final return to 0 fails the tests: `5 failed, 5 passed` (`t1_mutant.log`, exit 1). After restoring, there is no `MUTANT` marker.
- Ruff check and format exit 0. mypy: `Success: no issues found in 2 source files`.
- Masquerade and walker gates: `255 passed`, exit 0 (`t1_masquerade.log`).

## T2: the root comes before the cascade. Commit 50b25e90b.

- `test_withdrawn_reserve_proof_names_its_root_before_the_cascade` passes, together with its neighbours: the alias-escape test, and the two exact-`()` synthetic tests `test_architecture_scanner_detects_duplicate_move_replace_and_write_set_drift` and `test_resolver_cycle_guard_survives_the_callable_name_hop`. Result: `4 passed` (`t2_tests.log`).
- **Mutation control.** Dropping the root rows fails the new test (`t2_mutant.log`, exit 1). After restoring, there is no `MUTANT` marker. Ruff exits 0.

## Known-positive plant for T1 and T2

- **The current target cannot run the plant check.** At 582cc93c2 the escape scanners do not finish in practical time; see P0.
- **The check runs on a scratch tree instead:** `.claude/worktrees/u46-plantcheck`, detached at the pre-overlay `ae3904bd7` (escape 0), with T1 and T2 applied by `cherry-pick -n` and the plant added. It is not a branch and not committed. Queued behind the load gate; results go to `plantcheck.*`.
- **The baseline was already established** at `3b28d537b`: 3 escape rows from the old gate (diagnosis §5.4).

## P0: the retained_* memo. Stopped: no cheap exact fix. Needs an owner decision.

**Re-measured on 582cc93c2 itself.** Its gate is byte-identical to a6e812e86's (`git diff --stat` is empty).

| Measurement | Result | Log |
|---|---|---|
| Hot function on `tool_batch.py` alone, target gate (ac98fe243) | **2.9 CPU-s** | `subset_toolbatch_u46-target-ac98fe243.log` |
| Hot function on `tool_batch.py` alone, landed gate (582cc93c2) | **>241 CPU-s**, stopped by the alarm | `subset_toolbatch_u46-base-582cc93c2.log` |

**cProfile, 240 s on the landed gate.** `retained_enum_state` made 19,617,688 calls for 1,338 primitive calls. `retained_path_contains` ran 5.7M times (`subset_toolbatch.pstats`).

**Per-root instrumentation**, temporary and removed, with the tree verified clean afterwards:

- 550 root queries completed, each at most 42 body evaluations.
- **One root does not finish.** It is the Name `state` at `tool_batch.py:3758`, at 5,477,341 evaluations and still running (`rootcost.log`).
- It sits inside `_execute_continuation_call_live`, a 2,030-line function (lines 1754–3784) in the v152 rewrite, with one loop. `state` has 3 stores and 69 loads.

**The reviewed contract is binding.** `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`, lines 6850–7200, pins the two-witness memo:

- storage at most `2 × (N + 1)`;
- the latest witness plus one displaced witness;
- results equal to the no-reuse policy (`two == one == none`).

**Experiment: a multi-witness memo, exact by the same footprint argument.** It was uncommitted and has been reverted.

| Unit | Result |
|---|---|
| `schema_shape.py` alone | finishes in 44.1 s |
| `tool_batch.py` alone | still >175 s at the alarm (`mw_*.log`) |

So witness capacity is not the driver.

**Experiment: synthetic scaling with k mutually loop-carried variables**, k = 1–5 (`scaling.jsonl`). The two-witness memo grows linearly, 15 to 54 evaluations, and no-reuse grows quadratically, 41 to 430. Simple cycles therefore do not reproduce the blow-up.

**Reading (I).** The exploding query walks call arguments and child expressions across the data-flow graph of one very large function. The footprint-exact memo then sees many distinct (seen ∩ footprint) contexts. Any exact speed-up must keep equality with the naive cycle-cut DFS, as the contract tests require.

**Options**, which need the receipts' owner (carve-partition or the overlay author) or a maintainer ruling:

- **(a) A per-function SCC fixpoint evaluation.** This changes the semantics in cyclic, non-monotone cases (supplier-scalar suppression) unless that equality is proven.
- **(b) A fail-closed evaluation budget per root.** The gate goes red with "retention analysis budget exceeded at path:line". That preserves detection, but the target would be red until (a) or a refactor lands.
- **(c) Split `_execute_continuation_call_live`.** This is src owned by U-44b.

**Consequence.** Since the overlay landed, the escape test and the fencing tool cannot complete on the target in practical time. In effect the gate does not run at the target.

## COLD START (paused 2026-10-25)

### Units, branches and worktrees

All logs are under `.claude/lanes/release-0.8.10/u46fix/out/` unless another
path is given. Nothing is pushed or landed.

| Unit | Branch | Tip | Worktree | Base |
|---|---|---|---|---|
| U-46 fix | `codex/0.8.10-u46-fix` | `aa044516b` | `.claude/worktrees/u46-fix` | `582cc93c2` (the target is now `d7ad18596` or later; the branch is behind) |
| U-46 diagnosis doc | `codex/0.8.10-u46-diagnosis` | `6af0328e3` (§8 rev 2 at `a40c87fda`; fix-phase instruments at `6af0328e3`) | `.claude/worktrees/u46-doc` | `ac98fe243` |

- **U-46 fix commits:** T1 `a548991c6`, T2 `50b25e90b`, P1 `036a708f5`, (b) `aa044516b`. The (b) commit squashes WIP `10893144b` and follow-up `36dca5e94`. The old tip is pinned at `preserve/0.8.10-u46-b-wip`, and its diff against `aa044516b` is only the 3-line histogram comment.
- **U-46 diagnosis doc: UNLANDED.** The coordinator landed earlier sections. §8, the (a) design (rev 1 `bf7d75ba5`, rev 2 `a40c87fda`), is unlanded.
- **Scratch worktrees, all clean, detached, no commits of mine:** u46-base-582cc93c2, u46-p1snap (036a708f5), u46-bplant (36dca5e94), u46-plantcheck and u46-prep1 (ae3904bd7), u46-tip-ae3904bd7, u46-tip-feed6a9e6, u46-target-ac98fe243, u46-main-3b28d537b, u46-overlay-a6e812e86. Remove them with `scripts/worktree-cleanup.sh --path '<absolute glob>'` when no longer needed.
- **U-45 trees**, unchanged since the U-45 lane passed on: u45-profile (`codex/0.8.10-u45-fix-prototype` `5932af0e9`) and u45-doc (`codex/0.8.10-u45-diagnosis` `40f67d02f`).

### Done and verified

| Step | Evidence |
|---|---|
| T1 (exit status gates). Accepted by the coordinator. | `t1_tests3.log`, `t1_mutant.log`, `t1_masquerade.log` |
| T2 (root before cascade). Accepted. | `t2_tests.log`, `t2_mutant.log` |
| T1+T2 known-positive on pre-overlay `ae3904bd7`: exit 1, `GATE: RED`, the 3 baseline rows. | `plantcheck.log`, `plantcheck.json` |
| P1 equivalence on the target: 1,081 units, failure lists identical (318), 478,077 finished roots compared with 0 differences, 624 capped roots identical sets and not compared. Corpus instrument v2 (v1 missed the top-level `retained_callable_capture` root, my error, fixed). | `corpus_old.json`, `corpus_p1.json`, `corpus_compare.py` |
| (b) on `582cc93c2`: tool finishes, exit 1, `GATE: RED`, 1,083 CPU-s, 34.5 min wall at load 36–52, 961 budget rows. First row (T2): `reserve proof withdrawn: full source changed src/elspeth/web/sessions/service.py`. | `tool_b.log`, `tool_b.json` |
| Plant 1 (§5.4) red, 3 rows. | `tool_bplant.json` |
| Plant 2 inside `_execute_continuation_call_live` caught as `tool_batch.py:3759 ... callable escape .begin_run` when spelled `factory.run_lifecycle`, on a 1-unit run; 0 rows without the plant. | `plant2_subset.log`, `plant2_negative.log` |
| (b) budget test and mutation control. | `b_tests.log`, `b_mutant.log`, `b_squash_tests.log` |

### In progress at the pause, all stopped by me

- **The contract comparison**, `test_web_reserve_reviewed_contracts.py` base `582cc93c2` against (b) `aa044516b`, at `-n 4` with `--timeout 600`. Both runs stopped at 29%, with identical counts so far (42 passed, 225 failed, 51 errors). The id sets are **not comparable**: different subsets were reached, and my parse truncates parameter ids that contain spaces. **No conclusion.** Logs: `contracts_base.log`, `contracts_b.log`.
- **The fencing file at `-n 4` on (b)**, `suite_b.log`. Stopped at 43%. No timing obtained.

### Next steps, in order

**(b) landing.** Coordinator asks 1–3.

1. **Rebase (b) onto the target.** First `git -C .claude/worktrees/u46-fix update-ref refs/heads/preserve/0.8.10-u46-fix-pre-rebase HEAD`, then `git -C .claude/worktrees/u46-fix rebase release/0.8.10-assembly`. Expect conflicts only if the target touched the gate file; follow the merge-cost rule.
2. **Run the tool alone on the rebased tree**, one run at a time, at most `-n 3`, every run under timeout (AGENT-BRIEF "TEST CAPACITY — HARD RULES"):
   `cd <u46-fix> && PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src timeout 7200 .venv/bin/python scripts/fencing_inventory.py . --json <lane>/out/tool_b_rebased.json`
   The landing evidence is the **set** of path:line rows, not a count. Add the `582cc93c2` rows only where they explain a difference.
3. **Classify plant 2's miss, with evidence**, as the coordinator asked:
   - (a) Grep every `.run_lifecycle` receiver in `src/` and list each spelling.
   - (b) If a real or plausible spelling other than `factory.run_lifecycle` reaches a mutation and receiver detection misses it, record it as a gate blind spot and tell the coordinator.
   - (c) Otherwise, say at file:line why `batch` cannot carry `run_lifecycle`.
   Then redo the plant with the recognised spelling: the tool_batch.py:3758 insertion is `repo = factory.run_lifecycle; alias = repo.begin_run; callback(alias)` inside the finally block before `batch.state = state`, already shown caught on 1 unit.
4. **Landing-rule wording in the commit message.** The fencing ids move from hang to RED: no verdict before, a named red after. List the ids, at least `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` and `test_required_work_reserve_admission_on_actual_sources`; derive the full list from the rebased run. The contract comparison must show no other id turns red. Run it serially, `-n 3`, with `-rA --tb=line` and `--junitxml` so ids are exact, on base and (b). Contracts run with the budget disabled (autouse fixture present), and the budget is pinned in its own test.

**(a).**

5. **Revise §8 to rev 3** per `.claude/lanes/release-0.8.10/u46fix/mythos-a2-confirm.md`. Read it in full. Fold in its 3 blocking §8.5 fixes and every textual item from the coordinator's last message: the R5 None guard on the Call's own static origin, the per-element formal rule f(d), the Call static supplier-scalar early return, the store-ctx leaf, gating the retained branch of the A-false union on R(root), a product cap counted against (b), per-combination exemptions, capture-node R, R exactness in I3, the read_target cache key as actually passed, the corrected monotonicity argument, and the roots × budget bound. Commit it on `codex/0.8.10-u46-diagnosis`.
6. **Implement (a)** on `codex/0.8.10-u46-fix`, which the coordinator ruled needs no further full review. Re-derive (b)'s constant from (a)'s own evaluation counts in the same commit. Produce §8.8's evidence. Then one Mythos verification pass, via the coordinator.

### Open questions and rulings owed

- **Coordinator:** the plant-2 classification, if a blind spot is found, goes into (a) or gets its own fix.
- **Owner T5e, peer "0.8.10 - Assistant", `6c22638f3`:** re-pins the stale `sessions/service.py` full-source guard. Do not touch it.

### Hazards

- **KNOWN HANG.** The fencing test file cannot finish on any tree with the overlay unless (b) is present. Exclude it from directory-wide runs (AGENT-BRIEF).
- **The service.py full-source guard is stale** on `582cc93c2` (fa9e91575, d3d9bf308, 582cc93c2). Until T5e lands, the escape set has a `reserve proof withdrawn` root row and its cascade.
- **Contract-test ids** contain spaces. Parse them from junitxml, not from `-v` lines.
- **`git checkout --` is hook-blocked.** Use `git restore --source=HEAD`.
- **Plants are scratch only.** Never commit them.
- **The corpus and budget instruments must bound BOTH root entries** (retained_empty_root_state and the top-level retained_callable_capture).

### Landing conditions still owed

- The rebased row-set evidence.
- The plant-2 classification.
- The hang-to-red commit wording, with ids.
- The contract comparison showing no new red.
- The landing rule over (id, signature) sets across T1→T2→P1→(b).
- The gate-file whole-tree gates, which are masquerade and walker (`255 passed` at T1; re-run at the tip).
- For (a): §8.8's evidence, the dropped-row policy, and the budget re-derivation.
