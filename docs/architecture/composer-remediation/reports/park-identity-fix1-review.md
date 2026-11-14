# Park identity Task 2 — scoped fix-round 1 review

Verdict: **P2 ADDRESSED**. **Spec compliance: PASS. Task quality: PASS** for the requested correction. No new breakage found in the fix delta. This closes the single public-refusal oracle finding from `park-identity-review.md`; it is not combined-gate or merge approval.

## Scope and artifact binding

Reviewed the original P2 and its minimal correction, the Task 2 brief, the report's fix-round section, repository AGENTS.md and CONTRIBUTING.md, the complete changed test file, the exact delta, and the relevant preserved evidence. This review did not reopen the full Task 2 assessment or expand the provider-route requirement.

Current worktree HEAD was independently read as `05bcab93955b2070ffdc90e72eddd7368502d2c1`. SHA-256 values independently remeasured:

- Original reviewed artifact: `1a9999a08b9983d0cfb7a88cc508f1f9fab3518226d5c8e1487f55579c7a8616`.
- Fix delta: `f216c0000b189cac0bf65cd09062009c500098538bc2b282b6520b783ba1043f`.
- Final full artifact: `227c9c2795c1c426ee5eba865bd524ef0085c35590fe613efd50a9a71e3e8dca`.
- Production `graph_recovery.py`: `a0bc235eb91690b063fcef6e9533df623d136fda032b5c9b37887289330a101c`.

An independent complete-file comparison of the preserved reviewed test with the current test regenerated exactly the supplied one-hunk delta. The original and final full patches have the same seven file sections; only `tests/unit/web/sessions/test_graph_recovery_turn.py` differs between them. Each current touched file's Git blob ID matches the final artifact's new-blob ID. Reverse applicability of the fix delta passed without applying it. Current status lists the same seven tracked modifications and no untracked implementation files. Production changes are unchanged from the original reviewed candidate.

## P2 disposition

The corrected test pins the complete public assistant text and complete recovery-notice text to independently specified literals. Neither expectation depends on target ownership or selector spelling. The existing malformed-head suffix is the only conditional part of the expected notice. Therefore unknown and foreign targets must produce identical public text for each head condition.

The existing unknown/foreign × seven spellings × two head conditions matrix is retained. Both public texts explicitly exclude the prospective success message, canonical inaccessible ID, and original raw selector. The exact text assertions also reject extra distinguishing prose or a disclosure in either public field. The correction retains the real provider parsing and settlement, completed operation, unchanged-head inventory, private raw arguments, operation/epoch and physical-call binding, and public evidence filtering. The separate pending-proposal preservation tests are untouched.

No fixture, route, production behavior, planner behavior, or PostgreSQL test was changed in this fix. The original review's successful 42-case owned-restore route/dialect assessment remains undisturbed. Planner-refusal settlement remains the previously documented nonblocking coverage boundary; no additional route is required to close this P2.

## Evidence assessment

Read actual terminal exit files and logs, rather than relying only on report summaries:

- `fix1-refusal-final`: exit 0, **28 passed in 27.53s**.
- `fix1-ruff-final`: exit 0, **All checks passed!**
- `fix1-format-final`: exit 0, **1 file already formatted**.
- `fix1-mutant-old-oracle-controlled`: exit 0, **4 passed, 24 deselected**.
- `fix1-mutant-new-oracle`: exit 1, **4 failed, 24 deselected**. All four failures occur at `assert response.recovery_notice.content == expected_notice`, with the injected braced selector visible in the actual notice.
- `fix1-validation`: exit 0; its script and output were read.
- Original `affected-gates`: exit 0, **604 passed, 1 xfailed in 479.99s**. The logged xfail is the standing Sessions mutation-authority inventory classification diagnostic; this result does not claim that inventory is fully adjudicated.

Independent JUnit inspection confirms that the four controlled old/new cases have identical parameter names, all four also occur in the clean 28-case cohort, the old control has no failures/errors, and the new control has four exact-notice assertion failures rather than harness errors. The old-oracle file is byte-identical to the preserved reviewed test.

The actual mutant service was independently compared with candidate service bytes: it adds exactly `notice += f" Requested selector: {{{recovery.intent.park_id}}}."` after the common refused notice and matches its recorded before-run digest. In the selected braced cases this reconstructs the submitted raw alias. The preserved validation checks the tracked source-copy inventory and records that this is its sole source difference. The provenance records bind ordinary runs to the implementation worktree and mutant runs to the lane-copy `elspeth` and sessions service, with the worktree's separate lints root in both cases.

The initial outside-tests old-oracle diagnostic is correctly discarded: its exit is 1 and all four recorded failures report `Shared executor recovery callback absent`. The corrected command explicitly loads the existing root/unit/web conftest plugins. Only that corrected four-pass run qualifies the old oracle's acceptance of the disclosure mutant.

Original and fix-round before/after HEAD, diff-digest, and untracked-inventory files were independently compared and agree; both inventories are empty. The original freeze binds the terminal 604-pass cohort to the original reviewed artifact; the final freeze binds the focused checks and controls to the final formatted artifact. Thus the earlier pending-result/freeze condition is resolved without relabeling the original broad cohort as a rerun on the new test bytes.

## Limits

No tests, broad scans, providers, installs, implementation edits, commits, or subagents were run by this reviewer. Only this requested lane review was written. Parent-owned combined gates, the qualified all-rules baseline comparison, native CI, integration verification, and broader audit closure remain pending and unclaimed.
