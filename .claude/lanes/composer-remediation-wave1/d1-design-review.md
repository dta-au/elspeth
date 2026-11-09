# D1 design review

Read-only review of `6825994b482bc274df2ae85d8738b9078f0117b4`, the D1 recon, and the assessment README/remediation plan. No implementation or test execution. The user has decided direct refusal/no-op feedback, admission before staging, and terminal settlement of existing no-op proposals. Those decisions do not need reconfirmation.

## Verdict

The proposed split is sound, but the plan needs the four concrete additions below before implementation can claim complete D1 coverage. Most are implementation requirements, not new policy questions. Preserve provider-authored structure, existing required-control admission, exact proposal authority versus redacted display, and session/custody fencing. Do not add tutorial branches or a new lifecycle status.

### 1. Include canonical pipeline staging and settlement, not just legacy acceptance

`tool_batch.py:1916-1991` stages acceptable complete candidates without content-equality admission. `proposals.py:320-380` sends canonical proposals down a different acceptance path before the legacy no-op branch. `pipeline_commit.py:539-540,619-634` compares proposed/candidate/executor content but does not compare the successful result to current content; `pipeline_settlement.py:572-616` then builds and commits a state. An identical canonical `set_pipeline` can therefore escape a fix confined to incremental preview and `proposals.py:551`.

Add complete-candidate no-change admission and canonical pending-proposal terminalization explicitly. Compare final authority content, preserving meaningful source/branch ordering and review/provenance fields already covered by `composition_content_hash`; do not substitute redacted JSON or version equality. Canonical rejection must use the existing required-work/rejection-custody mechanism (`pipeline_settlement.py:494-511`), including dispatch recovery and cancellation. A direct call to the legacy rejection service in that branch would bypass its stronger custody contract. Preserve already-committed replay as committed; do not reinterpret a committed result as a pending no-op.

Tests needed: identical canonical public arguments and owned-state authority; changed controls/provenance as non-no-op; pending canonical no-op accept and repeat accept; persisted-dispatch recovery before settlement; cancellation during terminal rejection; no fabricated state row. Add these to `test_pipeline_proposal_lifecycle.py` and relevant pipeline commit/settlement fixtures, alongside the legacy route cases.

### 2. Blob admission must cover real driver prerequisites without invoking recovery

The recon correctly forbids `execute_tool(update_blob/delete_blob)` as a preview. The shared repository extraction is necessary but insufficient by itself. Replacement checks canonical file existence/type/hash before creating intent (`blobs/replacement.py:276-281`); relationally ready metadata alone does not establish admissibility. Missing/corrupt owned bytes remain operational/integrity faults, not an invitation for model-authored replacement. Reuse safe custody reads and exact operation-kind authority; do not use a read helper that implicitly repairs or retires obligations.

Quota includes **session, identity and container** bounds plus admitted output reservations. `repository.py:2351-2371` calls both session accounting and `RepositoryQuotaAuthority.admit_storage_bytes`; `quota_authority.py:808-912` can report revoked/unavailable accounting and records quota refusal evidence. Extract read-only quota evaluation rather than using only the session limit or suppressing the audit callback. Define “non-writing” as no resource/intent/reservation/effect write; ordinary audited attempt/refusal evidence still belongs in the audit path. Authority checks/locks alone are not a durable resource effect.

Tests needed beyond the recon matrix: missing/corrupt/symlink canonical bytes; identity and container limit independently; output reservations counted; revoked/unavailable accounting; wrong-kind, foreign-session and expired-fence authority. Assert no reconcile, replacement/deletion intent, byte write, reservation, metadata/provenance change or applied-effect receipt. Keep current delete-driver semantics outside D1: do not globally prohibit deletion of non-ready records merely because proposal staging requires ready metadata.

### 3. Both proposal-cap guards must move after semantic admission

There are two pre-admission guards: whole-batch requested-mutation counting at `tool_batch.py:1042-1054`, and the per-call guard at `1811-1815`. Moving only the first still prevents the model receiving a refusal/no-op once the staged count is full. D1 should not be declared complete with a known-refusal batch rejected as too many approvals. Keep any independent tool-call input limit, but enforce the pending-proposal budget only on stageable changes, before custody reservation/publication.

Tests: all-refused/all-no-op batch above the proposal cap; mixed batch with actual staged count at the cap followed by refusal/no-op; one additional valid change refuses at the cap; no inline custody reservation for a stageable candidate denied by that cap. Verify cross-provider tool-result pairing and one audit/outcome per attempted call.

### 4. Pin successful no-op feedback and terminal response semantics independently

Caching an unapplied result is the right approach, but `tool_batch.py:3336` currently treats every such result as anti-anchor failure. A successful no-op must neither fail an anchor nor clear it as graph progress. Preserve prior loop validation/preflight, original state/version, empty affected nodes and absent delta; do not label no-op `PREVALIDATION_REJECTED`. Pin downstream progress/repair-budget behavior as well as `success` and proposal count. Test finalization before comparison: required controls or review reconciliation may turn an apparently repeated authored edit into real content change.

Using existing terminal `rejected` with an explicit no-change reason/actor and structured 422 is a reasonable implementation choice. The frontend must reload that terminal result (`sessionStore.ts:1426` currently reloads only `proposal_validation_failed`), and retry must not redispatch. Keep refusal, argument error, content no-change, stale base, transient preflight failure and integrity fault distinct. An interrupted blob effect with a valid receipt is successful durable work despite unchanged graph hash; retain its existing receipt-first replay branch (`proposals.py:383-400,442-484`). A new no-op branch must not intercept it.

## Test-matrix additions and controls

The recon matrix is a strong starting point. Make its “pin every branch” commitment concrete with these additional cross-products:

- Empty patch, identical value, absent-key `null`, and actual one-field change for metadata/source/node/output; identical edge/replacement/complete pipeline. Include source and branch reordering as meaningful changes under the existing authority hash. A helper-only equality test is insufficient; test at least one real loop and each distinct acceptance route.
- Invalid current draft plus successful local no-op; invalid current draft plus locally valid repair that leaves other authoring errors. These pin whether D1 returns unchanged success or unapplied candidate diagnostics and prove no rejected candidate becomes the next call's baseline.
- Secret and blob markers: exact permitted policy and near-match denial, policy/metadata change after staging, no secret-value resolution, one production handler invocation at admission. Keep Q1 policy passing on acceptance as a prerequisite.
- Proposal-create race: named expected blob admission exception returns once with no row; unrelated `ValueError` and corrupt stored authority still fault. Current `proposal_blob_reference_ids` also raises `ValueError` for malformed nested authority, so narrow the domain exception by ownership/provenance rather than catching every `ValueError` in the service.
- Legacy malformed arguments and content no-op: one terminal event, no graph write, reload removes pending card, repeated accept cannot execute. Cancellation and competing terminal transition preserve the winning evidence; missing proposal rows remain integrity faults.
- Complete-candidate inline custody: retain all current reservation/retry/quota/finalizer exception controls. Same literal text is not sufficient to declare an inline source a no-op if blob identity/provenance changes; do not invent deduplication or orphan a reservation when adding the equality check.

Required validation follows the touched authority boundaries: focused real-loop/route tests, frontend refresh tests, then shared custody and PostgreSQL contention gates for repository/lock extraction. No gate was run in this review.

## Policy boundaries versus implementation choices

No new approval is needed for typed admission outcomes, canonical hash comparison, terminal rejected/no-change reason, error-envelope spelling, narrower admission exceptions or frontend refresh. D1 does not authorize changing auto-commit version semantics, D13 blob/base-staleness rules, D15 recovery supersession, or D35 splice replay identity. Preserve and test those boundaries.

The only material interpretation to state in the implementation plan is validation scope: existing tool/authoring admission and real blob/secret prerequisites, not a new requirement for Stage-2 runtime success before incremental proposals. The recon's instruction to return an otherwise successful candidate with authoring errors unapplied means an incomplete graph cannot progress through several individually approved partial changes; pin this deliberately against the user's “only changes passing validation-only checks” decision. If the parent intends a different partial-draft policy or universal runtime preflight, that is a policy expansion requiring an explicit ruling. Nothing in this review requires stopping the already-authorized D1 work.

## Follow-up: concrete implementation plan review

Reviewed `d1-implementation-plan.md` including the planner-publication addendum. **The plan addresses the four original design gaps and the third publisher.** Its corrected partial-draft policy supersedes the interpretation in the preceding paragraph: ordinary successful local changes may stage while unrelated authoring diagnostics remain; canonical `SetPipelineCandidate.acceptable` remains strict. This preserves existing handler admission and introduces no new Stage-2 gate. The matrix explicitly pins both changed partial repair and successful no-op on an invalid draft.

Parent confirmed the concrete route contract after review: preserve **ordinary immediate auto-commit state-method/version behavior**; D1 no-op admission and terminalization cover canonical/planner proposal paths under `explicit_approve`, `auto_commit`, and mid-plan downgrade. `_stage_pipeline_plan` is shared by those cases (`planning_application.py:674`). Align the plan's “preserve auto-commit behavior” wording to this precise scope before coding. This resolves the semantic ambiguity; no new user question is needed.

Add explicit required assertions for planner equal-content outcomes under `explicit_approve`, `auto_commit`, and mid-plan trust downgrade; pair each with a changed-plan control that retains existing trust re-read and Stage-2 behavior. Add a freeform auto canonical no-op control if that path produces an internal proposal. The current matrix's generic “auto behavior unchanged” row does not pin these shared-route interactions. Canonical pending malformed-argument terminalization is listed in the matrix: ensure T6 actually implements that outcome, rather than only the new no-change `PipelineCommitError` mapping, because its generic exception handler otherwise retains a pending proposal.

With that confirmed route contract incorporated into the plan and matrix, there is **no further blocking decomposition gap** identified in this review. The source/branch ordering, complete inline custody, quota/fence/no-resource-write oracle, capped mixed batches, canonical recovery evidence, terminal frontend reload and cancellation cases are now explicit implementation obligations. Every test named in the plan is either an existing test selected for reuse or a future required test. **This review ran no tests and establishes no passing implementation behavior.** Do not mark any matrix row complete until its production-path assertion and relevant controls have executed successfully.
