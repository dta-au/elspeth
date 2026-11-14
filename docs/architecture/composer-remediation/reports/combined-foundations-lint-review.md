# Combined foundations incremental keyless lint review

**Verdict: no actionable new defect in the four added findings or the 30 changed-location groups.** Keep the current control flow; no suppression, signature, allowlist, or metadata refresh is justified by this review. This is source review of the incremental corpus, not signed clearance or approval of the future final combined tree.

## Evidence and scope

Baseline: `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`, `d1-final-lints.json` (5429). Candidate: `b4064d2700bd4c9413a1cdcb794a95226681d373`, `combined-foundations-lints.json` (5433). HEAD was independently read as the candidate; production source and the base-to-candidate diff were read from those pinned commits. Read AGENTS.md, the original foundations brief, fix1 report, fix2 section of the current report, fix1/fix2 reviews, and the explicit reporting/release ruling at progress.md:244.

The saved command uses the integration worktree and both source roots, all rules and `src/elspeth`. Read actual scan exit `1` and frozen-exit `0`. The controlled comparator records four semantic additions, zero removals, 30 location groups, and passed positive/negative controls including multiplicity and semantic fields. Parent owns the full 6205-input freeze verification. This reviewer did not rerun tests or scans. Only this report was written; no source, index, installation, commit, Page, or metadata changes were made.

## Four additions: independently traced disposition

All line references in this section are to candidate `src/elspeth/web/execution/runtime_preflight.py`.

**R4, line 173, `except BaseException as failure`: justified outcome observation.** `_observe` calls `retained.task.result()` and retains the exact exception object at 174; it does not substitute a successful result. Its producers invoke it only for completed tasks: the done callback at 121–126, the `task.done()` branch at 151–155, and the post-`asyncio.wait` join at 197–202. The observed flag makes retrieval one-shot. A caller receiving an actual task failure re-raises the retained original at 155. A join owns its snapshot before its first suspension (234–237), waits for existing awaiters to settle (205–206), and collects genuine undelivered task failures by identity (207–208); seal raises them at 249–252. Completed abandoned failures outside a join are reported once at 193–195 and released under the explicitly approved bounded-history policy. `join_owned` prevents that report from replacing the join's failure delivery. Catching only Exception would miss actual task cancellation and other BaseException failures; removing the catch would lose this explicit observation/delivery routing. This is not silent success conversion.

**R5, line 176, `isinstance(result, RuntimePreflightFailure)`: justified nominal owned sum-type dispatch.** `RuntimePreflightEntry` is explicitly `ValidationResult | RuntimePreflightFailure` (39–45). `_capture` converts ordinary worker exceptions into that owned failure envelope (254–258); ordinary caller results are returned unchanged (157–159). The branch merely records the exception class for abandoned-result reporting. It neither structurally accepts a foreign object nor replaces malformed fields with defaults, and accesses `original_exc` directly. Actual thrown task failures take the preceding exception branch. Nominal discrimination implements the required difference between an ordinary result failure and a genuine cleanup/task failure, consistent with AGENTS.md's owned nominal typing policy.

**R6, line 243, `except asyncio.CancelledError as original`: justified deferred cancellation, not swallowed cancellation.** The first cancellation object is retained at 244–245 while `shield(task)` protects the shared join and its workers from caller cancellation. The loop continues until that join is done. At 246–248 the original cancellation is appended unless already present by identity; at 249–252 it is raised alone or grouped with distinct task failures. Repeated cancellation cannot overwrite the first object or prematurely release worker custody. `_join` has no worker-driven exception propagation from `asyncio.wait`: it observes and returns the failure tuple, so a normal worker abort cannot bypass the saved cancellation. This is the brief's explicit settle-then-propagate contract.

**R9, line 195, `_retained.pop(retained.task, None)`: justified idempotent bookkeeping release.** This is an owned task registry, not input/schema access. The guard at 184 requires eviction, observation, zero awaiters, and registry membership. `_release` has multiple legitimate callers: done callback, delivery, and caller-finally (126, 164, 181); later release attempts are expected. Admission installs the record before exposing the task (107–110), and lookup at 131 is strict. Thus the default does not paper over a missing required lookup or allow work to disappear before completion. The join retains the record objects in its separate tuple even after registry removal (234–237), preserving genuine pending join failures. A direct `del` would also work under the current membership guard; that stylistic simplification is not a correctness fix or reason to distort source to silence a heuristic.

The approved policy alone was not treated as proof: the delivery, reporting, release, and join paths above implement it. In particular, reported/released history is intentionally absent from later joins; unresolved join-owned failures remain deliverable.

## All 30 location groups checked against the actual diff

Read the complete production diffs for database.py, schema_probe.py, tool_batch.py, process_recovery.py, and process_watchdog.py. A read-only exact-line mapping of pinned old/new blobs matched every recorded occurrence to unchanged source text; duplicate occurrences were retained. The table records all groups in comparator order. These are unchanged finding sites, **not a claim that the surrounding files are behaviorally unchanged or all fingerprints moved only because of line numbers**.

| Group | File | Rule | Baseline lines → candidate lines |
|---|---|---|---|
| 1 | core/landscape/database.py | R1 | 1185 → 1205 |
| 2 | same | R1 | 238 → 258 |
| 3 | web/schema_probe.py | R2 | 205 → 205 |
| 4 | same | R2 | 203 → 203 |
| 5 | core/landscape/database.py | R4 | 2100 → 2120 |
| 6 | web/composer/tool_batch.py | R4 | 1842,2045,2150,2230 → 1836,2039,2144,2224 |
| 7 | same | R4 | 2673 → 2667 |
| 8 | same | R4 | 2920,3333 → 2914,3327 |
| 9 | web/process_recovery.py | R4 | 79 → 86 |
| 10 | web/schema_probe.py | R4 | 288,299,305 → same |
| 11 | same | R4 | 219 → 219 |
| 12 | core/landscape/database.py | R5 | 876,901 → 896,921 |
| 13 | same | R5 | 934 → 954 |
| 14 | same | R5 | 235 → 255 |
| 15 | web/composer/tool_batch.py | R5 | 3399 → 3393 |
| 16 | same | R5 | 3211 → 3205 |
| 17 | same | R5 | 2752 → 2746 |
| 18 | same | R5 | 2428 → 2422 |
| 19 | web/process_recovery.py | R5 | 19 → 18 |
| 20 | same | R6 | 76 → 83 |
| 21 | web/process_watchdog.py | R6 | 268 → 273 |
| 22 | same | R6 | 141 → 146 |
| 23 | core/landscape/database.py | R9 | 1361 → 1381 |
| 24 | same | R_TB_SUPPRESSED | 199,201 → 219,221 |
| 25 | same | R_TB_SUPPRESSED | 1340 → 1360 |
| 26 | web/schema_probe.py | R_TB_SUPPRESSED | 123 → 123 |
| 27 | same | R_TB_SUPPRESSED | 206 → 206 |
| 28 | same | R_TB_SUPPRESSED | 386 → 386 |
| 29 | same | R_TB_SUPPRESSED | 126 → 126 |
| 30 | core/landscape/database.py | R_TB_TESTS_FINGERPRINT_MISMATCH | 1254 → 1274 |

Source-grounded context for those mappings:

- **database.py:** the new serialized metadata-creation helper inserts 20 lines above every listed site; the three `create_all` calls change to that helper. Existing lookup/type-check/pop/decorator statements remain unchanged. Group 5's surrounding creation path genuinely changes, but its existing cleanup/re-raise handler remains intact; the helper itself restores constraint rules in `finally` and does not suppress creation failure. The existing test-fingerprint mismatch in group 30 remains an unresolved baseline diagnostic, with the same recorded and resolved hashes/message; moving its decorator is not clearance or a new mismatch.
- **tool_batch.py:** removing the hand-built cache payload and using the owned `to_dict()` representation yields net minus six lines before all listed findings. The listed handlers and nominal dispatch statements are unchanged. This is a real cache/audit behavior repair upstream, previously task-reviewed, not merely padding; no added catch/default or changed listed-handler behavior appears in the diff.
- **process_recovery.py:** removing `signal` shifts the constructor's nominal watchdog check by minus one. Adding `_force_after_failure` and migrating to semantic termination produces net plus seven before `_join_owned`. That entire join method is unchanged, including its deferred cancellation and eventual raise/group logic. The new helper preserves the original supervision failure and groups distinct forced-termination failure; it does not silently normalize failure.
- **process_watchdog.py:** replacing the abstract signal operation with two semantic methods adds five lines before both recorded catches. Their bodies are unchanged. Concrete adapters added later call the existing SIGTERM/SIGKILL implementation, retaining the same Linux signal path.
- **schema_probe.py:** all eight groups retain their original line numbers and statements while fingerprints differ. Imports and the missing-schema creation call change to the serialized Landscape helper, with no net line shift. Those are file/context-binding changes, not line-number movement. The pgcode/sqlstate parsing, invalidation/re-raise paths, trust-boundary statements and pool classification remain unchanged; the initialization path genuinely changes its creation helper and retains its existing failure cleanup.

No changed-location group is evidence of a newly added defensive fallback. No existing baseline warning/error is hereby adjudicated away, and a context/fingerprint change still requires the ordinary operator binding review if later signed clearance is requested.

## Limits and next action

The parent should retain these four source-grounded dispositions alongside the raw red corpus and perform its final all-rule comparison after all combined changes. This review covers the specified baseline-to-b406 incremental findings only. It does not establish signed allowlist clearance, completion of D2/portable supervision, native-platform qualification, or a final combined scan. No additional source fix is requested by this incremental lint review.
