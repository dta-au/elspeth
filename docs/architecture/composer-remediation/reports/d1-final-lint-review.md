# Final D1 lint-delta adjudication

Verdict: **all five added occurrences are justified; no source repair required.** No new Critical, Important, or Minor defect was found in this delta. This written adjudication does not suppress findings, alter signatures, or make the nonzero corpus signed-clear.

Reviewed at unchanged HEAD `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1` on 2026-11-09. Independently checked HEAD and clean worktree status. Read `d1-final-lint-comparison.json`, selected actual records from `d1-final-lints.json`, and the relevant baseline records from `d1-lints-before.json`. The comparison contains **5424 before / 5429 after**, **five added occurrences across four semantic keys**, and **zero removed occurrences**. R4 and R6 have existing occurrences with the same semantic key, so source/diff inspection was used to identify the additional sites rather than treating every current location as new.

This was source/evidence inspection only. No scanner, test, source/index/HEAD edit, or metadata/signature change was performed. Read complete relevant helpers and their concrete caller/producer dependencies; the larger `run_tool_batch` and acceptance-route context was also reviewed in the preceding combined review. Existing test results retain the freeze/scope qualifications recorded in the task reports and `d1-final-review.md`; no new execution is claimed here.

## 1. CEC1 — internal blob-admission tool selector

Location: `src/elspeth/web/composer/tools/blobs.py:1743`, fingerprint `fb74df983e822a19`.

**Disposition: false positive for the LLM-argument channel invariant; retain the internal fail-fast guard.**

The complete `admit_blob_mutation` function accepts only update/delete operations and raises this `ValueError` when its internal operation selector is anything else. The sole production call is `tool_batch.py:2116`, immediately dominated by `tool_name in {"update_blob", "delete_blob"}` at line 2113. It passes that same local string into the worker. Neither `_execute_update_blob` nor the delete handler calls this helper, and this helper is not a registered tool handler. An unsupported model tool name therefore cannot reach this guard through the live call path.

Actual model arguments are validated separately by `_validate_mutation_arguments`, `_validated_blob_update`, the UUID validator, and `_blob_update_bytes`; argument failures use typed argument errors or failure results. The flagged guard instead detects internal misuse. Converting it to `ToolArgumentError` would misclassify such misuse as something the model can repair.

The CEC1 implementation in `elspeth-lints/src/elspeth_lints/rules/composer/exception_channel/rule.py` explicitly treats functions with no module-local caller as escaping, because it cannot inspect an external caller's guard. Its bounded nominal-invariant exemption does not understand this string-membership precondition. The finding is consequently expected from the implemented analysis, but it does not establish the prohibited argument-failure path.

Existing evidence: `test_durable_blob_mutations_admit_and_stage_without_executing_effect_handler` at `tests/integration/web/composer/test_freeform_proposal_prevalidation.py:2235` exercises both admitted selectors and forbids destructive preview; the refusal matrix at line 2280 exercises both selectors' direct feedback. Blob admission tests cover real arguments, metadata, custody, and corruption. There is no claim of a dedicated test for an invalid internal selector; the unreachable-live-path conclusion follows from the complete production call-site search and guard, not from those positive tests.

## 2. R4 — blob admission exception retained for audited re-raise

Added location: `src/elspeth/web/composer/tool_batch.py:2150`, fingerprint `f2faa598c27710fa`.

**Disposition: false positive for exception swallowing; retain explicit exception custody.**

The baseline has three occurrences of this semantic key at lines 1802, 1989, and 2119; their current counterparts are 1842, 2045, and 2230. The new fourth occurrence is the blob admission catch at 2150. It stores the original exception object in `preproposal_exception` and clears publication arguments. It does not return success, fabricate a refusal, invoke recovery, or retry the destructive handler. The following generic preview excludes the blob-store effect domain, so it cannot overwrite this saved blob exception by executing the mutation.

The shared `_do_dispatch` captures that object at line 3136 and **raises it at line 3139 before any tool dispatch or cached-result return**. `dispatch_with_audit` in `composer/audit.py:1069` then distinguishes `ToolArgumentError`, normal faults, and cancellation/shutdown signals and records exactly once in its `finally` before propagation. Catching `BaseException` at the admission boundary is intentional: cancellation and other original signals must reach that audited exit as well. The R4 message only sees that the lexical catch body has no raise; it misses the explicit later re-raise.

Existing evidence: the shared preproposal path is exercised by `test_preproposal_base_exception_is_audited_once_and_propagated_unchanged` at freeform-prevalidation line 1298, which asserts object identity, one invocation, and no proposal/state/blob write. Adjacent runtime-error and prior-validation tests pin fault classification. The blob-specific tests at lines 2235 and 2280 pin the effect exclusion, no-write admission, one feedback/outcome, and real proposal/refusal paths. These are complementary evidence; the generic BaseException test is not described as injecting a failure specifically into the new blob helper.

## 3. R5 — select the owned deletion contract's paths

Location: `src/elspeth/web/blobs/service.py:2941`, fingerprint `d180e6d99231b4bf`.

**Disposition: justified nominal union discrimination; retain.**

`_require_blob_deletion_recovery_content` accepts the explicit owned union `BlobDeletionPlan | BlobAtomicDeletionObligation`. These are different frozen contracts, not alternative shapes guessed from external data. The atomic obligation represents an already committed metadata deletion with paths/timestamps and no operation-qualified hash evidence. The qualified plan includes operation, phase, and exact content evidence. The first check chooses the correct path validator and artifact set for that contract.

`coordination/repository.py::inspect_blob_mutation` obtains this union from the authoritative readers. `read_atomic_blob_deletion` validates the atomic all-NULL evidence schema, rejects live metadata, constructs the owned obligation, and raises integrity errors on malformed evidence. Partially qualified rows are sent through the qualified parser instead of acquiring atomic authority. This `isinstance` is therefore nominal dispatch after validation, not an attempt to hide a bad owned object with defaults.

## 4. R5 — apply atomic versus qualified recovery invariants

Location: `src/elspeth/web/blobs/service.py:2954`, fingerprint `d47bba5cb0e33dde`.

**Disposition: justified nominal union discrimination; retain.**

The second check in the same complete function applies the atomic contract's postcommit invariant: canonical bytes must be absent, the custody directory must remain stable, and no hash claim is invented. The qualified branch instead applies phase/presence and recorded size/hash checks. Merging the branches would either demand evidence the atomic producer never records or drop the qualified producer's stronger invariants.

Both R5 sites follow AGENTS.md's nominal-owned-type guidance and CONTRIBUTING.md:706–727, which explicitly allows `isinstance` where the negative union arm matters and warns against rewriting code merely to satisfy the lint. These checks use a concrete owned class, not a runtime structural Protocol or a defensive shape probe.

Existing evidence for both sites: `tests/unit/web/blobs/test_mutation_admission.py:262` creates genuine interrupted atomic and qualified producer debt at each supported phase, asserts unchanged resources and a recovery-needed refusal, then performs separate real recovery as a positive control. The corruption matrix at line 325 includes atomic live-metadata/path violations and qualified metadata/path/identity/byte corruption and requires integrity faults with unchanged resources. Tests at lines 817 and 889 cover supported artifact alternatives and absent-file debt without inventing bytes. They exercise the actual contract distinction these two checks implement.

## 5. R6 — a competing terminal transition is an explicit outcome

Added location: `src/elspeth/web/sessions/routes/composer/proposals.py:106`, fingerprint `24fed8b1aecbe2af`.

**Disposition: false positive for silent swallowing; retain the narrow conflict outcome.**

The baseline occurrence at line 618 corresponds to current line 709. The new site is `_reject_legacy_proposal_refusal`, whose complete body catches only `ProposalStateConflictError` and returns `False` to mean this call did not own the terminal transition. Both callers consume this boolean: lines 553–580 for typed argument refusal and 616–643 for no-change. They preserve deferred cancellation and raise an explicit 422 response that says the proposal is already terminal and asks the client to reload, instead of claiming automatic rejection.

`sessions/service.py::reject_composition_proposal` documents and implements the distinction: a missing row raises `KeyError`; exact prior rejection checks its bound event; a different terminal decision/actor raises `ProposalStateConflictError`; pending rejection performs the authoritative transaction. The helper does not catch missing-row, audit-integrity, or arbitrary `ValueError` faults. The boolean is an explicit local control outcome consumed by the HTTP error path, not a swallowed error followed by success.

Existing evidence: `tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py:380` races real acceptance/rejection transitions and asserts the winning actor/event, truthful terminal response, unchanged winner state, and no extra event on retry. Line 546 injects missing-row, integrity, and unexpected value faults and asserts original exception identity plus unchanged pending state/events. Cancellation/cleanup cases at lines 422 and 496 cover original custody. The behavior is therefore tested beyond a mocked boolean return.

## Required action and limits

No repair, suppression, allowlist expansion, signature refresh, or additional test run is required for these five occurrences. Retain this written rationale alongside the exact nonzero corpus. The prior combined source-review verdict stands; this report explicitly adjudicates the newly supplied lint delta rather than treating five added findings as baseline churn. Parent-owned combined backend and serial PostgreSQL runs remain outside this review's execution scope, and their final results are not asserted here.
