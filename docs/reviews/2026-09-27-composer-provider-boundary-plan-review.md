# Composer provider boundary: Astra plan review

Date: 2026-09-27
Target: `release/0.8.1`
Plan: [Provider boundary remediation](../plans/2026-09-27-composer-provider-boundary-remediation.md)
Diagnosis: [Provider boundary gap](2026-09-27-composer-provider-boundary-gap.md)

## Final plan verdict: GO

Astra reviewed the plan against the provider dispatch, exception taxonomy, session audit, quota admission, cancellation, and test requirements. Approval covers the revised design and implementation work. It does not establish implementation correctness, passing gates, merge readiness, or deployment status.

## Review decisions

### Round 1: NO-GO — quota refusal after a settled failure

The original acceptance criteria promised a provider-error response after retry exhaustion without accounting for quota admission. Settling a failed call with unknown token usage leaves NULL usage in the ledger. Daily accounting remains unknown, so the next retry can be refused before dispatch. This is established by `quota_authority._daily_token_total_on_connection`, chargeable admission's `TOKEN_ACCOUNTING_UNAVAILABLE` branch, and `test_timeout_with_unknown_usage_blocks_next_physical_call`.

The required revision was to preserve that refusal, retain unknown usage, and distinguish it from provider exhaustion. Zero pending attempts cannot prove that accounting is known or that subsequent admission will succeed.

### Round 2: GO — quota-enabled and quota-free outcomes separated

The revised plan resolved the blocker by requiring:

- With quotas: a valid first call, a real second-call `ServiceUnavailableError`, two terminal audits, settled attempts, unknown usage retained, and refusal before a third dispatch. No third call may be fabricated.
- A distinct accounting-unavailable HTTP 503 and failed progress with administrator guidance, shared by send-message and recompose. Immediate retry must not be presented as a remedy.
- A fresh-operation check proving that settled unknown usage still blocks admission.
- Without quotas: bounded provider retries and the normal provider-failure HTTP/progress outcome.

Provider retry eligibility remains subject to deadline, attempt cap, and fresh quota admission. The classifier, narrow unexpected-exception auditing, route translation, caller inventory, cancellation audit, and proposed gates were otherwise accepted.

### Round 3: GO — cancellation custody and SDK timeout addendum

Implementation-stage source review identified additional defects at the same interface: cancellation can lose the result of a committed admission worker; settlement failure can replace an earlier cancellation; and recompose can release its lease while shielded audit/progress tasks remain unjoined.

The approved design requires the caller to shield and join the actual admission task, obtain its result, and retain the original cancellation. A committed intent that never entered the provider SDK requires an explicit fenced cancellation disposition. That disposition must atomically record distinct cancellation evidence and settle proven zero usage, without creating a `ComposerLLMCall` or successful provider result. The existing schema requires a ledger reference for a settled attempt, so this design needs a service/protocol API extension but no schema extension. It must be idempotent, reject conflicting evidence and ownership, and apply to manual auto-title admission as well.

Zero usage is valid only when the SDK was never entered. Cancellation after dispatch, timeout, lost responses, and historical unknown attempts retain unknown usage. Failure to persist the disposition leaves the pending row fail-closed.

Settlement must join its child task on every path. A secondary ordinary cleanup failure should be chained beneath the original cancellation; integrity failures remain visible. Recompose must join audit and progress tasks before releasing authority, preserving disconnect and heartbeat behavior. Deterministic tests must cover repeated cancellation, child failure/self-cancellation, and cancellation concurrent with completion.

The addendum also correctly classifies `openai.APITimeoutError`, including LiteLLM `Timeout`, before the general SDK fallback. Planner and ordinary call tests must prove TIMEOUT audit status, no automatic retry, safe timeout outcomes, and unknown usage after dispatch.

### Round 4: GO — typed advisor END failure classification

Task 5.4 is consistent with the approved provider-caller sweep. The advisor END verdict previously used exception class names to distinguish an unavailable provider from malformed output. A first-party exception with the same name could therefore receive outage wording without establishing SDK provenance.

The approved correction uses the shared owned classifier and concrete transport exception types. It preserves the advisor's narrower policy: a genuine SDK service-unavailable error or timeout receives unavailable wording, while a generic API error retains the malformed verdict. This does not change advisor admission, blocking, or degradation policy. Tests must include real SDK 503/timeout classes, a generic API error, and a first-party same-name impostor that remains unclassified as an outage. The route owner reports RED/GREEN evidence for those controls; this plan addendum review did not independently rerun them. Final implementation review and gates remain required.

## Signoff limits and required evidence

The reviews inspected source and the installed SDK; they did not run tests or inspect deployed incident records. Implementation must pass the plan's focused behavioral tests, relevant whole-tree gates, frozen full gate, PostgreSQL persistence checks, and independent implementation review. The final cancellation audit must report actual results and unresolved paths.

Gateway error-code/request-ID correlation remains an explicit investigation item. Class-based handling alone does not prove preservation of gateway metadata, identify the AWS cause, or reconcile historical pending attempts. Those limits must remain clear in the completion report.
