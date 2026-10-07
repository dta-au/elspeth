# Actual Daybreak generation-unavailable POLICY GO — 2026-10-06

Native CLI metadata identifies `gpt-daybreak-blue-latest`, provider `openai`, read-only sandbox, approval `never`, session `01a111c0-4576-72b2-9f4d-1d82871462b1`. The process completed exit 0 and returned narrow POLICY GO resolving the four prior policy blockers. All 13 immutable technical payload members retained their exact SHA256 identities after review. This snapshot was captured during implementation and supplies no final source or test clearance. It contains two normative technical documents, the complete preserved first technical policy verdict, and ten relevant source/test files; controlled scope/conversation/credential-pattern checks passed.

The mapper may now be implemented under the reviewed contract; implementation, complete positive/negative/mutation proofs, final frozen gates and independent source review remain outstanding. The earlier amendment PLAN GO and John's LOCAL-testing HOLD remain separate. The first POLICY NO-GO and all original inputs are preserved. The reviewer titled its response 2026-10-07; executor clock recorded completion on 2026-10-06 UTC. Its complete body is preserved below without correcting that date, with only snapshot-local links normalized to repository-relative references. Raw response, native log, exit and input remain unchanged in task custody.

# Actual Daybreak narrow policy rereview — 2026-10-07

## Verdict: POLICY GO

The revised narrow `RequiredGenerationUnavailable` rank-20 policy resolves all four blockers from the preserved POLICY NO-GO.

This is policy clearance only. It does not provide source clearance, test clearance, frozen-gate clearance, merge/deployment approval, or LOCAL-testing authorization. The public mapping remains correctly held because the supplied implementation is unfinished.

The earlier amendment PLAN GO is unchanged.

## Scope and method

I reviewed the two complete normative design documents, the preserved prior POLICY NO-GO, and the included source/test snapshot. I did not use conversation material, runtime data, archives, environment/secrets, network access, tests, provider calls, deployments, writes, or nested models.

Because nested models were prohibited, I applied the four lenses directly:

1. Normative consistency
2. Nominal source taxonomy
3. Public-boundary parity and cause handling
4. Lifecycle/custody semantics and proof obligations

## Blocker disposition

### 1. Ambiguous nominal taxonomy — RESOLVED

`RequiredGenerationUnavailable` is now confined to operational generation refusal.

The complete source emitter inventory contains five raising sites:

- unresolved existing generation recovery;
- previous shared generation not physically joined;
- lifecycle unavailable because quarantine, permanent draining, or equivalent closure applies;
- generation no longer active when submission is rechecked under its lock;
- shared-executor shutdown already started.

These appear at [async_workers.py:79](../../../src/elspeth/web/async_workers.py:79), [async_workers.py:84](../../../src/elspeth/web/async_workers.py:84), [async_workers.py:126](../../../src/elspeth/web/async_workers.py:126), [async_workers.py:175](../../../src/elspeth/web/async_workers.py:175), and [async_workers.py:336](../../../src/elspeth/web/async_workers.py:336).

Excluded defects now have the correct taxonomy:

- Missing recovery registration is `AuditIntegrityError`, not operational unavailability: [async_workers.py:94](../../../src/elspeth/web/async_workers.py:94).
- Missing finalizer owner or non-draining state is `AuditIntegrityError`: [async_workers.py:232](../../../src/elspeth/web/async_workers.py:232).
- Foreign, unsealed, unregistered, substituted, or reused capabilities fail through nominal integrity checks: [application_finalizers.py:64](../../../src/elspeth/web/application_finalizers.py:64).
- Unclaimed and reinvoked capabilities fail as integrity defects: [application_finalizers.py:34](../../../src/elspeth/web/application_finalizers.py:34).
- Registration after sealing or duplicate-kind registration is an integrity defect: [application_finalizers.py:50](../../../src/elspeth/web/application_finalizers.py:50).
- Invalid finalizer kind and direct construction without the private creation seal retain fail-fast `TypeError`.
- Invalid required-ticket type remains fail-fast `TypeError`.
- Invalid boolean, non-finite, non-positive, or otherwise invalid drain configuration retains `ValueError`: [async_workers.py:77](../../../src/elspeth/web/async_workers.py:77).

A valid, claimed finalizer may bypass only the ordinary permanent-draining admission refusal. It cannot bypass `_GENERATION_UNAVAILABLE`, an inactive/quarantined generation, shared shutdown, or an invalid owner/capability check. This is the required operational-versus-integrity distinction.

### 2. Closed operational emitter set — RESOLVED

The revised policy declares the emitter inventory closed and matches the supplied source inventory. New emitters require policy review; call-site inference, exception-message parsing, stack inspection, caller-selected categories, and callback-selected ranks are forbidden.

The accepted operational states are exactly:

- quarantine;
- closed/shared shutdown;
- permanent process draining;
- unresolved recovery or old-generation ownership/join.

No missing-registration or invalid-finalizer arm belongs to this set. The normative closure is explicit at [collision-reducer-design-2026-10-06.md:130](collision-reducer-design-2026-10-06.md:130).

### 3. Exact three-surface public parity — RESOLVED

The policy now requires exactly these three public surfaces:

1. `required_work`: owned leaf extraction and rank-20 reduction;
2. `composer_operation_errors`: owned leaf extraction, category selection, and detached projection;
3. the mounted application exception handler.

All three must return HTTP 503 with the existing fields:

```json
{
  "detail": "Database is currently unavailable. Please retry in a moment.",
  "error_type": "database_unavailable",
  "request_id": "<existing correlation value>"
}
```

The mounted `RequiredGenerationUnavailable` handler must be separate from SQL-specific diagnostics. It must not inspect `.orig`, SQL state, pool status, or perform a database query. It may record bounded nominal operational diagnostics, but it must not expose exception text.

The established body is visible at [app.py:2314](../../../src/elspeth/web/app.py:2314) and the detached equivalent at [composer_operation_errors.py:158](../../../src/elspeth/web/sessions/composer_operation_errors.py:158). The revised contract freezes parity at [collision-reducer-design-2026-10-06.md:132](collision-reducer-design-2026-10-06.md:132).

No rank-90 kind is added. The six-kind rank-90 enum and all existing envelopes remain unchanged.

### 4. Closed owned-cause policy — RESOLVED

Cause traversal is now restricted to the existing explicitly owned wrappers:

- `ComposerOwnedSettlementFailure`;
- `ComposerRequiredRecoveryFailure`;
- the existing owned cancellation wrapper.

A direct nominal `RequiredGenerationUnavailable` leaf may survive through those wrappers and original exception groups. The wrapper, original root, group, cause, and every receipt remain retained evidence.

The policy explicitly forbids promotion through:

- arbitrary `RuntimeError`;
- generic `TimeoutError`;
- SDK/provider exceptions;
- unowned HTTP or generic wrappers;
- message adjacency or words such as “shutdown,” “database,” “generation,” or “timeout”;
- a generic exception whose cause is `RequiredGenerationUnavailable`.

The existing extraction points that require the narrow addition are [required_work.py:547](../../../src/elspeth/web/required_work.py:547) and [composer_operation_errors.py:103](../../../src/elspeth/web/sessions/composer_operation_errors.py:103). The revised closed rule is normative at [collision-reducer-design-2026-10-06.md:134](collision-reducer-design-2026-10-06.md:134).

## Rank and evidence assessment

The accepted ordering is:

- rank 10: nominal audit/integrity failure;
- rank 20: `OperationalError`, `AsyncWorkerAdmissionTimeoutError`, or the corrected nominal operational `RequiredGenerationUnavailable`;
- ranks 30–100 unchanged.

Consequently:

- audit 500 deterministically defeats generation 503;
- generation 503 defeats storage, other SQL, settlement, Stop, deadline, custody-loss, rank-90 public, and unclassified failures;
- same-rank collisions use the canonical immutable receipt key;
- receipt or exception-group arrival order cannot choose the winner;
- no completion timestamp, random identifier, task identity, dictionary order, or exception text becomes a tie-breaker;
- every original root, cause, exception group, witness, and nonwinning receipt remains available as deterministic secondary evidence.

This is explicitly frozen at [collision-reducer-design-2026-10-06.md:106](collision-reducer-design-2026-10-06.md:106) and [collision-reducer-design-2026-10-06.md:142](collision-reducer-design-2026-10-06.md:142).

## Lifecycle and custody assessment

The policy does not authorize a custody shortcut.

A rank-20 result does not prove rollback, callable non-entry, terminal failure, lease loss, or old-generation completion. It authorizes none of:

- submission retry;
- provider retry or replay;
- START replay;
- SQL retry outside existing exact immutable authority;
- terminal-publication retry;
- lease-release retry;
- premature generation replacement;
- readiness reopening before measured old-generation completion.

Pre-submit refusal must execute the callable zero times and release only the structurally unused admission. Returned futures remain joined and observed. No-return anomalies retain quarantine and the existing aborted-wrapper or measured-generation-drain proof. Sequential replacement remains forbidden until the old executor’s workers have joined. Escalated process draining remains permanent within the old process.

The supplied custody tests cover portions of those existing mechanics, but they are not evidence for the future public mapping and were not executed in this review.

## Source status

The source is deliberately unfinished:

- `required_work._category` does not yet recognize `RequiredGenerationUnavailable`: [required_work.py:568](../../../src/elspeth/web/required_work.py:568).
- Required-work owned cause extraction does not yet admit the nominal type: [required_work.py:547](../../../src/elspeth/web/required_work.py:547).
- Detached extraction/category/projection does not yet admit it: [composer_operation_errors.py:103](../../../src/elspeth/web/sessions/composer_operation_errors.py:103).
- The mounted handler is absent; only the existing `OperationalError` handler exists at [app.py:2293](../../../src/elspeth/web/app.py:2293).
- The included tests establish selected taxonomy and custody behavior, not three-surface mapping parity, complete collision semantics, or mutation resistance.

These are expected implementation gaps, not policy blockers.

## Mandatory future positive integration proofs

Implementation clearance must prove:

- Every closed operational emitter produces nominal `RequiredGenerationUnavailable`.
- Ordinary refusal for quarantine, shutdown/closed, permanent draining, and unresolved old-generation ownership returns the exact 503 body.
- Required-work refusal at the same boundary reduces at rank 20.
- Mounted, detached, and required-work projection agree exactly on status, detail, `error_type`, and request-id correlation.
- A valid claimed finalizer encountering genuine quarantine/generation closure receives the operational refusal only after its authority checks pass.
- Rank 10 defeats rank 20.
- Rank 20 defeats ranks 30, 40, 50, 51, 60, 70, 80, 90, and 100.
- Same-rank collisions with `OperationalError` and `AsyncWorkerAdmissionTimeoutError` use canonical key order.
- Reversing receipt arrival and exception-group order leaves the winner and public envelope unchanged.
- Original roots, leaf identities, wrappers, causes, groups, witnesses, and secondary receipts remain unchanged and deterministically ordered.
- Pre-submit refusal invokes the callable zero times and releases exactly one unused admission.
- No provider or SQL work is performed or replayed.
- No fabricated future, Stop, deadline, fence loss, lease loss, or terminal result is created.
- Quarantine/readiness remains closed until measured old-generation join permits sequential replacement.
- Escalation prevents same-process replacement permanently.

## Mandatory future negative integration proofs

None of the following may produce `database_unavailable` 503:

- absent recovery registration;
- absent application-finalizer owner;
- non-draining finalizer invocation;
- foreign owner;
- unsealed, unregistered, substituted, wrong-kind, unclaimed, reused, or already-invoked capability;
- duplicate or post-seal registration;
- impossible invocation witness;
- invalid finalizer constructor seal or kind;
- invalid required-ticket type;
- boolean, non-finite, non-positive, or wrong-typed scalar configuration;
- arbitrary `RuntimeError`;
- arbitrary SDK/provider error;
- generic `TimeoutError`;
- an unowned wrapper whose cause is nominal `RequiredGenerationUnavailable`;
- a message mentioning generation, shutdown, database, or timeout;
- finalizer callback failure;
- recovery callback failure;
- unknown submit/setup failure lacking the nominal operational object;
- cancellation, durable Stop, deadline, fence loss, or lease loss alone;
- callback-returned, producer-selected, message-derived, or caller-supplied category/rank;
- any retry or replay caused by selection of the 503 envelope.

## Mandatory mutation proofs

Controls must fail if a mutation:

- restores any defect emitter to `RequiredGenerationUnavailable`;
- maps every `RuntimeError` or `TimeoutError` to rank 20;
- traverses arbitrary cause chains;
- omits any of the three public surfaces;
- changes status, detail, `error_type`, or correlation treatment;
- routes the mounted handler through SQL-only diagnostics;
- introduces a new rank-90 kind;
- places rank 20 ahead of rank 10 or behind any rank 30–100 category;
- chooses by group/arrival order;
- drops or mutates original evidence;
- releases admission without unused/future/aborted/joined proof;
- permits callable execution after an aborted gate;
- retries submission, START, provider, SQL, terminal publication, or lease release;
- installs or readies a replacement before old-generation join;
- clears quarantine or permanent draining after escalation.

## Final disposition

**POLICY GO.**

The revised policy is nominal, closed, deterministic, parity-complete, and consistent with the existing custody model. The four previous blockers are resolved.

The mapper may now be implemented under this policy, but remains held until implementation and the complete positive, negative, mutation, integration, and frozen-gate proofs are supplied. No final source clearance is granted.

Skill applied: `using-software-engineering` → `code-review-methodology`, adapted directly through the four requested lenses without subagents.
