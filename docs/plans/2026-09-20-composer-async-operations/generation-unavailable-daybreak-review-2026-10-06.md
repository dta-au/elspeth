# Actual Daybreak generation-unavailable policy review — 2026-10-06

Native CLI metadata identifies `gpt-daybreak-blue-latest`, provider `openai`, read-only sandbox, approval `never`, session `01a111ab-5c1b-7c40-9515-14a27bdf22e6`. The process completed exit 0 and returned narrow POLICY NO-GO because the supplied nominal exception conflated operational refusal and integrity defects. The earlier amendment PLAN GO remains valid for independent work; the new public mapping remains held. All 11 frozen technical payload members retained their exact hashes after review. This source was captured during implementation and is not final frozen-source evidence. The payload contained only two streaming design documents and nine relevant source/test files, with controlled scope/conversation/credential-pattern checks; no conversation excerpt, authorization evidence, archive, task log or runtime data was included.

The complete verdict follows with only snapshot-local links normalized to repository-relative references. Raw response, native log, exit and input remain unchanged in task custody. The emitter taxonomy, closed policy/parity and controls must be repaired and reviewed before mapping implementation. Final source checks, independent review and local-testing HOLD remain separate.

## Actual POLICY verdict: NO-GO

The intended policy is sound, but the proposed rank-20 addition is not yet implementable as a nominal-type rule against the supplied source.

`RequiredGenerationUnavailable` currently represents both:

- genuine operational admission refusals; and
- conditions the proposed policy explicitly says must remain integrity defects.

Mapping that nominal class globally to `database_unavailable` HTTP 503 would therefore create an integrity-to-availability downgrade. Mapper implementation must remain held.

This is a narrow policy NO-GO only. It does not revoke the earlier plan GO or stop other approved work. Final frozen gates, final source clearance, and LOCAL-testing remain HOLD.

## Four-lens review

No nested models were used because they were expressly prohibited. I applied the reviewer method directly through four lenses: normative consistency, source taxonomy, lifecycle semantics, and proof obligations.

### 1. Normative consistency

The desired classification is internally coherent:

- Rank 20 remains ahead of storage 30, generic SQL 40, settlement 50/51, Stop 60, deadline 70, custody loss 80, typed public failures 90, and unknown failure 100.
- It reuses the fixed `database_unavailable` 503 envelope.
- It introduces neither a rank-90 kind nor a new response body.
- It does not authorize retry, replay, provider re-entry, fabricated lease loss, timeout conversion, callback defaults, or exception-selected rank.
- The existing receipt key remains the deterministic tie-breaker after category rank.
- The original exception can remain the original category witness and original root.
- Stop, deadline, and worker-loss semantics remain unchanged; a genuine operational refusal simply has the already-declared stronger rank 20.
- Constructor validation can remain fail-fast `TypeError`/`ValueError`.

Thus the policy would be GO if the exception taxonomy identified only the operational boundary.

### 2. Blocking source-taxonomy conflict

The nominal exception is defined as a plain `RuntimeError` at [required_executor.py:19](../../../src/elspeth/web/required_executor.py:19). Its current operational uses are appropriate:

- existing recovery or old generation still owns admission at [async_workers.py:80](../../../src/elspeth/web/async_workers.py:80);
- lifecycle/quarantine refusal at [async_workers.py:116](../../../src/elspeth/web/async_workers.py:116);
- post-admission generation closure at [async_workers.py:166](../../../src/elspeth/web/async_workers.py:166);
- shared-executor shutdown at [async_workers.py:326](../../../src/elspeth/web/async_workers.py:326).

But the same class also currently names defects that the proposal explicitly excludes:

1. Missing recovery registration is emitted as `RequiredGenerationUnavailable` at [async_workers.py:120](../../../src/elspeth/web/async_workers.py:120).

2. Missing finalizer ownership or invalid non-draining finalizer state is emitted as `RequiredGenerationUnavailable` at [async_workers.py:220](../../../src/elspeth/web/async_workers.py:220).

Because reducer and projectors see exception objects rather than the exact raising branch, they cannot safely distinguish these cases without inspecting text, stack location, mutable state, or caller-selected metadata. All of those would violate the nominal, deterministic policy.

Invalid finalizer capability/ownership must likewise remain an integrity refusal, regardless of whether it is detected by `owner.claim`, `_submission_unavailable`, or a future validation branch.

### 3. Mapper and boundary parity

The mapper is currently absent from all three required surfaces:

- reducer `_category` at [required_work.py:548](../../../src/elspeth/web/required_work.py:548);
- detached leaf/category/projector paths at [composer_operation_errors.py:101](../../../src/elspeth/web/sessions/composer_operation_errors.py:101);
- mounted application exception handlers around [app.py:2293](../../../src/elspeth/web/app.py:2293).

That absence is expected while implementation is held. Once the taxonomy blocker is resolved, all three must use the same fixed projection:

```json
{
  "detail": "Database is currently unavailable. Please retry in a moment.",
  "error_type": "database_unavailable",
  "request_id": "<existing correlation value>"
}
```

The mounted handler must not perform database diagnostics requiring an `OperationalError`, expose exception text, or route through generic HTTP details.

Cause extraction at [required_work.py:523](../../../src/elspeth/web/required_work.py:523) and [composer_operation_errors.py:101](../../../src/elspeth/web/sessions/composer_operation_errors.py:101) must recognize only the corrected nominal operational refusal. It must not promote arbitrary `RuntimeError`, SDK, provider, or nested generic causes.

### 4. Lifecycle, custody, and ABI assessment

The addition itself does not require changing physical custody:

- admission is released as unused only before submission;
- returned futures remain joined and observed;
- no-return submissions remain quarantined and non-replayable;
- replacement remains sequential after measured old-generation join;
- generation-drain expiry remains process-recovery evidence rather than a timeout result;
- terminal, lease-renewal, and lease-release work retain their existing barriers;
- no 503 response may imply rollback, lease loss, deadline, or terminal failure.

The following existing validation ABI must remain outside the new mapping:

- invalid drain scalar: `ValueError` at [async_workers.py:77](../../../src/elspeth/web/async_workers.py:77);
- invalid required ticket: `TypeError` at [async_workers.py:241](../../../src/elspeth/web/async_workers.py:241);
- invalid watchdog ownership: `TypeError` at [process_recovery.py:18](../../../src/elspeth/web/process_recovery.py:18);
- invalid factory-owned watchdog/settings checks in the app remain their present fail-fast types.

## Exact blockers before POLICY GO

1. Make the nominal taxonomy unambiguous.

   Either:

   - change missing recovery registration and invalid finalizer owner/state/capability paths to nominal `AuditIntegrityError` subclasses; or
   - introduce a distinct operational admission-refusal class and reserve it exclusively for the approved generation states.

   Message inspection, traceback inspection, callback-selected category, source-line classification, and code-selected rank are not acceptable substitutes.

2. Enumerate the allowed operational emitters.

   The closed set must cover only ordinary/shared admission refusal caused by:

   - quarantined generation;
   - closed or shutting-down generation;
   - permanent process draining;
   - unresolved ownership/join of the old generation.

   A valid owned shutdown finalizer refused because the generation is genuinely unavailable may use the operational type. Invalid finalizer authority or state may not.

3. Specify parity across reducer, detached projector, and mounted HTTP handler.

   All must emit the exact existing fixed database-unavailable envelope. None may expose `str(exc)` or create a new public kind.

4. Specify cause handling explicitly.

   The corrected nominal operational exception may be retained as a recognized leaf through owned settlement/recovery wrappers. Arbitrary `RuntimeError`, SDK, provider, or HTTP causes must stay rank 100 or their existing rank; they must never inherit rank 20 merely from adjacency or chaining.

## Mandatory positive integration proofs

After the blockers are resolved, the implementation must prove:

- Ordinary `run_sync_in_worker` refusal for each approved state—quarantined, shutdown/closed, permanent draining, unresolved old-generation ownership—returns the exact fixed HTTP 503 body.
- Required-work refusal at the same shared admission boundary reduces at rank 20 and produces byte-equivalent public fields to the ordinary boundary.
- Detached projection and mounted HTTP handling agree on status, `error_type`, detail, and request-id treatment.
- Rank-20 collisions are invariant under reversed receipt arrival and reversed exception-group order.
- Rank 20 deterministically defeats ranks 30, 40, 50, 51, 60, 70, 80, 90, and 100 while retaining every original root, leaf object, cause, and secondary receipt.
- Same-rank collision with `OperationalError` and `AsyncWorkerAdmissionTimeoutError` follows the existing canonical key order, never group order.
- Refusal before submission releases only unused admission, invokes the callable zero times, creates no provider/SQL replay, and does not fabricate a future, lease-loss signal, Stop, or deadline.
- A valid finalizer encountering genuine generation closure receives the approved operational refusal without bypassing owner/capability checks.
- Shutdown/readiness remain closed while the generation/process latch is set and do not reopen before the existing measured join rules allow it.

## Mandatory negative integration proofs

The controls must prove that all of the following do **not** produce `database_unavailable` 503:

- recovery callback was never registered;
- finalizer owner is missing;
- application is not in the required finalizer state;
- capability is foreign, unclaimed, already consumed, wrong-owner, wrong-kind, or otherwise invalid;
- malformed generation/finalizer ownership or impossible witness state;
- boolean, non-finite, non-positive, or wrong-typed constructor/configuration scalars;
- invalid required-work ticket type;
- arbitrary `RuntimeError`;
- arbitrary SDK/provider exception;
- generic exception whose cause happens to mention generation, shutdown, database, or timeout;
- generic `TimeoutError`;
- finalizer callback failure;
- unknown submit failure without a nominal operational-refusal object;
- cancellation, durable Stop, deadline, fence loss, or lease loss presented alone;
- any attempt to select rank by callback return, message text, call-site code, or caller-supplied category;
- any path that retries submission, provider work, START, SQL, terminal publication, or lease release because a 503 was selected.

Mutation controls must fail if the implementation:

- maps every `RequiredGenerationUnavailable` before separating defect emitters;
- omits any of the three projection surfaces;
- introduces a rank-90 kind or alternate body;
- promotes generic causes;
- converts timeout/cancellation into generation unavailability;
- releases custody without the existing unused/future/aborted/joined proof;
- admits replacement work before old-generation join.

## Disposition

The narrow public behavior is acceptable in principle, but the current nominal class is semantically overloaded. Therefore:

**POLICY NO-GO. Mapper remains held.**

A resubmission can earn POLICY GO by first making operational refusal nominally distinguishable from missing registration and invalid finalizer authority/state/capability, then freezing the three-surface parity and proof matrix above. No final code clearance, frozen-gate clearance, deployment clearance, or LOCAL-testing authorization is supplied.

Skill applied: `using-software-engineering` → `code-review-methodology`, adapted directly without subagents.
