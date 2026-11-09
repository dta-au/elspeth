# D1 state admission review

Spec verdict: **APPROVE for the state-admission and proposal-cap tranche in `d1-state-admission-brief.md`.**

Code-quality verdict: **APPROVE.** No actionable correctness, regression, authority, or policy-weakening findings in the frozen diff. This is not approval of integrated D1 or a green release gate.

Reviewed the brief, report, T1/T3 and scope provisions of the implementation plan, repository AGENTS/CONTRIBUTING guidance, the complete patch, surrounding dispatch/finalization/effect-registry code, behavioral tests, and recorded verification artifacts. No tests were run and no product/test files were edited during this review.

## Artifact identity

Read-only verification returned HEAD `66739807c9acbdae6f2d7d9f1de858b8e69c9793` and exactly the five expected staged paths. The existing untracked frontend `node_modules` entry remains separate. Review artifact SHA256 is `91379cf76399ac5d38de03c6d6c27c334c666129dc3f8b50ab45cf9f7b1fa8ac`.

Live file SHA256 values matched both supplied freeze manifests:

```text
f3fd019f95e4ae1233a32673696ae3bb71957f605c01ff712fa815eba3873939  src/elspeth/web/composer/tool_batch.py
ba921b1b2421c14e518190fbdc5eac982df8b2495fe5716fbda446ea559ae65f  tests/integration/web/composer/test_freeform_proposal_prevalidation.py
f5aa05a6da88154aa8936e1bd7b9f72edb5b21e67116fccc11eaee532791693d  tests/unit/web/composer/test_proposal_cap_turn_counts.py
18a4ea10b196306c2f885b7c1609d8828aa2baf62988031f9405ee04f96d2e5c  tests/unit/web/composer/test_tool_batch_rejection_llm_call_audit.py
dddae38e9a207c6e3715dbdb4643d82984b075a7e228ffc75439ab9d4a04f4ac  tests/unit/web/composer/test_tool_result_envelope_gate.py
```

## Spec assessment

- `tool_batch.py:216-249,2090-2170,3057-3065`: frozen `_UnappliedAdmission` distinguishes successful no-change from refusal. Ordinary admission uses the actual `execute_tool` path with policy, session operation/fence, secret policy/service, blob quota, user/model provenance, schema validation and interpretation/profile checks. Registry `EffectDomain.BLOB_STORE` excludes durable tools while including state-only markers. The first actual result or exception is retained and reused inside canonical audited dispatch, without handler replay. Exception capture is not swallowing: dispatch re-raises it through the existing argument-error/crash authority.
- `tool_batch.py:2129-2152`: required controls run under the existing successful/version-advanced/globally-valid finalization conditions, before content comparison. This condition is not an incremental staging gate: successful changed partial-draft repairs still stage. `execute_tool` performs the ordinary interpretation/profile normalization before this seam. Canonical finalization at `733-805` preserves review authority and `candidate.acceptable`; equality at `1929-1941` does not admit an otherwise rejected candidate.
- `tool_batch.py:228-246,3124-3131,3323-3349,3392-3395,3415-3427`: unapplied results retain original graph/version and runtime observations; successful no-change uses standing validation, clears affected nodes/delta/echo, carries truthful unchanged feedback, and neither increments nor clears anti-anchor progress. Refused canonical candidates retain their repair diagnostics even when their historical result has `success=True`; refusal disposition drives failure accounting and blocker completion. Immediate ordinary auto behavior is outside the new admission branch.
- `tool_batch.py:1943-1950,2176-2184`: both requested-mutation cap guards are replaced by post-admission checks, including a check before inline custody preparation. Refusals/no-ops remain deliverable at the proposal cap. A changed eleventh candidate creates no eleventh proposal or inline resource. The independent input call-budget machinery is untouched.
- `tool_batch.py:417-451,1000`: current-batch invocation slicing preserves the actual audited prefix without copying earlier persisted discovery invocations. Original `ctx.failed_turn`, turn counters, carrying completion, and evidence are preserved. The report's description is truthful: cap+1 pure preview is observed, but no staged/audited dispatch or synthetic outcome is invented for it. “Not attempted” in the nearby legacy terminology must be read as not admitted to that dispatch path, not as no preview execution. The report expressly makes that distinction.
- No provider-owned authority bypass was introduced. Provider `set_pipeline` still enters the existing public candidate/schema authority. The owned representation is created only through the trusted incremental required-control seam. Public singleton/named-source equal candidates are tested here; owned canonical acceptance/planner routes remain explicitly assigned to the follow-on work. They should not be represented as covered by the public-loop equality tests.

## Tests and policy preservation

The real compose/provider harness covers both dialects for refusal/argument/no-op followed by a repair, meaningful mutation-family equal/change controls, invalid-draft standing observations, anti-anchor neutrality, required-control staging, marker policy/metadata prerequisites, unchanged auto version behavior, ordered-source authority, inline blob identity, and durable update/delete exclusion. Cap tests assert first-ten staging and no eleventh blob. The turn-count test preserves failed-turn identity and prior counts while requiring exact current-prefix call IDs.

The changed structural assertion in `test_tool_result_envelope_gate.py:3780-3880` follows the real nominal helper and verifies the canonical rejected candidate actually uses its refused disposition and original state. It retains the original state/delta/affected-node protections and adds the applied-component prohibition. Each of four AST negative controls first checks the unmodified assertion and then removes one required change field, requiring the actual assertion failure. No generic walker, constructor inventory, generator, fence, attribution, or wire permission was loosened by this patch.

The supplied base-source oracle replaces only the exact base `tool_batch` module in memory and, for the envelope selection, its AST reads. The report accurately labels that method and excludes malformed early fixtures from valid red evidence. Its terminal logs and exit files agree with the reported results:

| Recorded check | Terminal evidence |
| --- | --- |
| Focused integration/cap/marker/control tests | exit 0; 153 passed |
| Service selection | exit 0; 50 passed, 220 deselected |
| Final frozen integration/progress/audit/cap/structural selection | exit 0; 112 passed |
| Base-source behavioral control | exit 1; 37 failed, 9 passed, 24 deselected |
| Final Ruff, formatting, scoped mypy, contracts | each exit 0 |
| Response/envelope group | exit 1; 403 passed, **14 failed** |

Both import roots in the recorded provenance point into the worktree. The report correctly distinguishes the earlier broad response run from the subsequent one-line refused-progress fix and final 112-case selection.

## Remaining boundaries

The 14 envelope failures are the exact recorded pre-D1 failure identities. They are our earlier `f598aa43d` diagnostic-expression scanner regression, as explained in `baseline-envelope-gate-analysis.md`, and are not an upstream fault. The current report states that accurately. The group remains red; it also masks generic inventory/negative-control assurance until the separately queued scanner repair passes. Approval of this tranche does not certify those checks.

Non-writing update/delete store prerequisites and quotas, proposal-create races, planner/canonical publication across trust modes, and pending canonical acceptance terminalization are explicit follow-on ownership, not silently claimed fixes in this patch. Full/static/corpus/PostgreSQL integration gates remain parent-owned. These boundaries are consistent with this task's narrower brief and must remain visible in the combined D1 completion claim.
