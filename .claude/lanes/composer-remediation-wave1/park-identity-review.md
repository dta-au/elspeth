# Park identity Task 2 independent review

Verdict: **CHANGES REQUESTED**. The production implementation matches the bounded identity design, but one required outward-refusal behavior is not meaningfully pinned by the tests. This is a test-oracle finding, not an observed production disclosure or ownership defect. The final affected-gate cohort and after-run freeze receipt were still pending when this review was issued; no PASS or merge-readiness claim is made.

Spec-compliance verdict: **CHANGES REQUESTED** — production behavior matches the Task 2 design, but the required indistinguishable outward-refusal regression oracle is incomplete.

Task-quality verdict: **CHANGES REQUESTED** — the implementation is appropriately scoped and the evidence is candid, but the P2 oracle gap must be corrected; final affected-gate completion and freeze verification remain pending.

## Reviewed candidate and method

- Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-park-identity`.
- Base/current HEAD independently read: `05bcab93955b2070ffdc90e72eddd7368502d2c1`.
- Complete reviewed patch: `park-identity-review.diff`, SHA-256 `1a9999a08b9983d0cfb7a88cc508f1f9fab3518226d5c8e1487f55579c7a8616` (remeasured immediately before writing this review).
- Read the brief first, the implementation report, the complete 520-line patch, park requirements in `resource-identity-design-review.md`, AGENTS.md, CONTRIBUTING.md, all seven touched files, and relevant unchanged parsing, ownership, qualification, settlement, receipt, and provider-test dependencies.
- Read existing evidence and explicit exit files; independently categorized the negative-control JUnit results. No tests, providers, scans, installs, implementation edits, commits, or subagents were run by this reviewer. Only this requested lane review was written.
- Read-only status showed the seven reported tracked modifications and no untracked implementation path. The supplied before-freeze head/diff digest matches the reviewed artifact; a final before/after freeze conclusion remains pending.

## Finding: P2 — pin complete, indistinguishable public refusals

Location: `tests/unit/web/sessions/test_graph_recovery_turn.py:139–142`, in `test_refused_provider_recovery_retains_exact_private_control_and_physical_binding`. Related internal-only oracle: `tests/unit/web/sessions/test_composer_recovery_security.py:105–117`.

Task 2 explicitly requires unknown and foreign park aliases to retain `target_unavailable` with **indistinguishable outward refusal**, alongside unchanged head and pending proposal. The current provider test runs both target kinds as separate parameter cases, but only excludes the prospective success message and canonical inaccessible ID from the assistant text, then checks that the separate recovery notice contains `did not complete`. It does not compare or pin either complete public text. The preparation test compares the internal `failure_reason` only.

Consequently, a response that says a foreign draft exists but is inaccessible versus an unknown draft being absent would satisfy these assertions. So would a notice containing the raw braced/uppercase alias followed by the required substring. The current canonical-ID absence assertion does not inspect the notice and need not match a differently spelled alias in the assistant message. These are concrete examples of forbidden outward behavior the required oracle presently accepts.

Minimal correction: retain the existing alias × target-kind × malformed-head matrix and assert complete public assistant and recovery-notice content against the same independently specified expected refusal for both target kinds. Include the existing malformed-head suffix only when appropriate, or explicitly compare the two target-kind outcomes after removing incidental message IDs/timestamps. Check that neither the canonical inaccessible ID nor the original alias appears in either public text. Keep the existing real settlement, unchanged-head, private raw-argument/physical-binding assertions and the separate pending-proposal preservation matrix. A narrow negative control that varies foreign/unknown public text or injects an alias should make the new oracle fail. No production change or extra provider route is needed for this finding.

Static code currently uses the same refusal branch: `composer_turn.py:1065` chooses the trusted refusal message, and `sessions/service.py:9297` builds the common notice, with the existing malformed-head suffix. That supports the implementation but does not replace the requested regression assertion.

## Specification assessment

The four added production lines are correctly located in `GraphRecoveryIntent.__post_init__`. The existing exact-string, raw-length, and nonblank checks run before stripping and parsing. Only restore selectors are projected; invalid/non-UUID text remains byte-for-byte unchanged, and messages/tool-call IDs retain their whitespace. The shared helper adds no version restriction. `object.__setattr__` is a frozen-construction assignment, not an added dynamic attribute probe.

The change reaches direct service callers and provider parsing without a route-specific branch or second persistence normalization. `recovery_controls.py` retains strict argument models, the message credential guard, sole-call batching, and raw provider capture. `prepare_graph_recovery` retains the session-filtered park query, operation authority checks, target qualification, and head checks. Stored park identities still pass through exact `recovery_identity` validation; owned corruption is not sanitized. The refusal settlement returns before pending-proposal invalidation. No D15 policy, schema, receipt, stored proposal, hash, or graph-content migration was introduced.

Success coverage meets the requested route/dialect requirement: the actual provider matrix covers seven spellings × freeform/planner/malformed-head × both dialects. It asserts an advertised exact target, one provider request, a new state derived from that target, graph-recovery provenance, no execution validity/completion metadata, unchanged authored message, and retained raw provider facts. The real park/create/later-restore harness also exercises actual persistence and settlement.

Planner refusal settlement is not covered by the new refusal matrix: it covers freeform and malformed-head. The existing planner recovery tests stop at `PlannerHandoff`, rather than settling a foreign/missing target. This is a nonblocking coverage boundary: the explicit all-route/dialect requirement accompanies the owned successful roundtrip, and that requirement is met. This review does not request an additional cross-product.

## Quality and evidence assessment

The case-bearing UUID fixture removes vacuous uppercase/mixed selectors. Its numeric-seed control checks distinct outputs and changed selectors that parse back to the intended identities. The autospecced UUID patch is scoped to the park-preparation call; the test still uses real persistence, authority, and terminal settlement. It changes UUID generation only, rather than manufacturing a successful lookup or settlement.

The received-evidence test selects by original provider call facts, which is appropriate for the existing planner `unknown_tool` category. It does not relabel production evidence or claim to fix that adjacent observation. Original aliases remain in private arguments while the owned intent is canonical.

Read evidence confirms:

- Corrected provider matrix: exit 0, 42 passed. Final fixture-focused selection: exit 0, 29 passed, 70 deselected.
- Real serial PostgreSQL selection: exit 0, 28 passed across own roundtrip, own/foreign/unknown preparation, and refusal/pending preservation. This is not the full PostgreSQL selection.
- Negative control: exit 1, 48 failed / 8 passed. Independent JUnit categorization found constructor 6 alias failures / 1 canonical pass, persistence 6 / 1, and provider 36 / 6. All 36 provider failure texts contain the actual unavailable-target notice; canonical planner controls pass. This establishes a meaningful identity mutant, not merely a parser-field assertion or failed fixture setup.
- Final touched-file Ruff and format checks, scoped mypy, and contracts: explicit exit 0. The affected-gates terminal result is pending.
- Original red, planner cost-fixture errors, evidence-filter errors, and the interrupted void run are disclosed in the implementation report. They are not counted as candidate green evidence or uniformly intended red behavior.

The three-rule boundary diagnostic retains the reported 205 occurrences / 204 distinct findings with a controlled Counter comparison and no added/removed occurrence. Its before-all-rules source freeze limitation remains explicit. This is not all-rules clearance; the parent owns the final combined comparison against its earlier frozen baseline. No key, suppression, signing metadata, wire declaration, or baseline pin was changed.

## Disposition

Resolve the single P2 oracle finding, obtain the still-running cohort's terminal result and freeze receipt, and re-review the test-only delta against that result. No blocking production defect was found in this candidate. Full default behavior coverage, full serial PostgreSQL coverage, long proofs, final combined all-rules adjudication, and integration compatibility remain parent-owned and unclaimed here. The review does not close broader R2/XC-13, pagination, general observation, or umbrella audit findings.
