# D1 gate-repair fix1 scoped rereview

**Spec verdict: PASS for this fix. Code-quality verdict: PASS for this fix.**

**Original P2 truthful-progress finding: ADDRESSED.** No new actionable finding in the scoped delta. This disposition closes the finding in `d1-gate-repair-review.md`; it does not reopen the original broad review or establish full D1/gate closure.

Reviewed the appended fix-round report and scoped diff against the previously reviewed candidate. Independently verified:

- Scoped diff SHA-256: `4e2c3e58d630e5d9f7f826dfa70a3a66d3c7ec394e033dba8b55772e422e8ada`.
- Full candidate diff SHA-256: `30012bc28f227c3e3bf7258f014e9f94c869654129151bdbcf4cce129402e3d9`.
- Comparing the original and final full-diff sections shows the same eleven paths and exactly one changed section: `tests/unit/web/composer/test_compose_loop_persistence.py`. Candidate: `/workspace/elspeth/.claude/worktrees/composer-remediation-d1-gates-fix1`.

## Finding disposition and focused checks

The former positive-count assertion is replaced by complete ordered event equality. `_record_real_batch_progress` observes actual producer events through the real emitter and forwards to the original sink; it does not fabricate or suppress producer events. The metadata ten/eleven cases require exactly the request-intent event. Reused-ID replay must add no event. The real blob tenth/eleventh cases and malformed-at-cap case also pin the complete sequence. Thus an extra started/completed event or an applied/validating claim fails. The mixed schema/semantic test independently pins the canonical started/blocker sequence, including the distinction between a successful candidate producer and an unapplied refused result.

Read the complete mutation launcher and the terminal mutation receipts. `completed-denied` compiles the real batch function in memory and injects real successful completion emission at both cap raises; exactly metadata `[11]` and blob `[admitted-eleventh]` fail, with four controls passing and no setup/teardown errors. `applied-batch` changes the real batch event to an explicit completed/applied claim; all six selected cases fail without setup/teardown errors. These controls directly exercise the original finding. The launcher restores imported objects in `finally`; the restoration receipt contains 1,609 `OK` entries and no other nonempty lines.

The existing batch headline remains in-progress model-authoring language accompanied by “A pipeline-editing tool was requested” and future validation before saving. The complete pinned tuple, unchanged state/version checks and absence of completion events distinguish that announcement from a claim of saved application. This fix does not conceal a demonstrated production defect by altering production copy.

Read the complete amended capture helper and three affected tests, then checked the real wire codec, missing-required-path branch, canonical candidate/refusal branch and redaction producers. The optional feedback observer reads the next provider request's tool messages without changing them. Existing callers remain unaffected when observation is omitted.

The semantic fixture now uses production `encode_semantic_arguments`, whose complete implementation supplies the required `{"pipeline": ...}` frame. It supplies an existing CSV under `tmp_path/blobs/<session-id>/` and an observed schema; the actual Composer fixture uses that same `tmp_path` as its data directory. The final feedback records confirm candidate validation errors `no_sinks_configured` and `source_on_success_dangling`, rather than a wire-envelope or path-policy argument refusal. The test checks `PREVALIDATION_REJECTED`, `applied=false`, original version/empty affected nodes, invalid stored validation, ordered exact tool-call identities and the nine real proposal publications.

The changed stored-feedback expectations preserve meaningful assertions on the correct surfaces. Missing-required-path provider feedback is an error-only object; the stored ARG_ERROR projection carries the closed class/category. Semantic refusal's raw `data` mapping carries status/application facts while storage redacts that mapping. The admitted deletion's raw approval is correlated to the actual pending proposal ID/tool identity; its stored projection retains success/version/affected nodes plus the unknown-response sentinel. Inspected final complete observation records agree with these producers and assertions. No redaction, admission or provenance production check is changed to fit the fixtures. Existing cap prefix, absent denied feedback/invocation, graph-publication and exact blob-resource assertions remain.

## Verification boundary

Read terminal logs and exit receipts: 102 selected tests passed, final formatting-followup six passed, and the final endpoint/gateway selection passed 25. All three final freeze receipts report `frozen=true` for 6,204 tracked inputs. The two mutant processes exited 1 with the exact intended failures described above; those are deliberate negative controls, not green suite results. Parent separately validated the formatting-only AST equivalence and final tracked-input receipts.

I ran no tests, scans, git commands or installations and changed only this report. The original 211-case authority/source-analysis run remains pending for the parent to disposition. The separately reported PostgreSQL result of 1,068 passes/one failure remains under independent diagnosis and is outside this scoped rereview. Neither this approval nor the focused green receipts constitute whole-tree, PostgreSQL or full D1 closure.
