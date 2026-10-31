# Exception-management scope for 0.8.10

Release planning record, 20 October 2026. This document contains public-safe source observations only. Private incident payloads and diagnostic evidence are kept separately and must not be included here.

The 0.8.10 block groups the remaining 0.8.7 and 0.8.9 work, correctness, Composer+, and a specific exception-management pass. These are release-grouping requirements, not authorization to start every stream. Composer+ is the user's term; equivalence with other named continuation work has not been established.

The urgent request-rejection/reason-preservation correction is a separate 0.8.9 increment candidate and has priority. Its inspected source base is `b09f5419764b69a1761dbce856f32fa96fde20a6`; that source binding is not confirmation of an increment's target branch or exact version. The existing release owner must establish those before integration. No merge or deployment is implied by either grouping.

## Required behavior

Exceptions must reach their responsible handler and no shorter or further. Keep semantic type, original causal chain and useful safe reason until the handler makes an explicit disposition. Separate private internal exceptions from safe serializable descriptors. Expected refusals and cancellation must preserve security, publication, custody and audit behavior. Keep one responsible diagnostic/logging owner; do not duplicate raw exception logging. A controlled completed operation may still contain a blocked review and must not be presented as approval.

## Initial boundary matrix

This is a bounded source review of the listed paths, not an exhaustive catch inventory or release qualification. Baseline is `b09f5419764b69a1761dbce856f32fa96fde20a6`. Follow-on hypotheses remain unverified until focused offline reproductions establish reachability and expected ownership.

| Origin and semantic type | Causal custody and responsible handler | Recovery/retry | Diagnostics and safe user form | Coverage / next check |
| --- | --- | --- | --- | --- |
| Provider SDK request rejection (`BadRequestError`) | SDK -> provider audit -> Advisor checkpoint -> terminal completion owner; keep original internally, project one safe descriptor | Nonretryable unchanged request; completion/publication withheld | Closed reason code/disposition, fixed explanation, existing call-derived reference; no raw body/message/param | Urgent increment: offline mapper/checkpoint/gate/wire regressions; actual gateway producer contract remains a separate prerequisite |
| Structured reason lost during SDK wrapping | SDK mapping may retain HTTP response while dropping SDK body/code | Recover only from bounded already-buffered declared response JSON; never fetch or scrape prose | Distinguish safely retained, absent, unrecognized, and unavailable evidence | Urgent increment: actual installed mapper exercised with synthetic responses; deployed SDK/gateway parity unverified |
| Advisor hint declared provider failure | Advisor owner -> typed internal tool outcome -> tool dispatcher | Preserve existing degradation and accounting; no fabricated guidance | Same safe rejection descriptor; private cause stays outside tool JSON | Urgent increment candidate includes the shared reason-bearing boundary; requires tool-path regression |
| Provider failure coincident with budget refusal | Checkpoint -> budget controlling outcome with underlying original failure retained | Existing budget refusal still controls; no extra attempt or publication | Preserve safe underlying reason without relabeling budget as review approval | Urgent increment candidate: targeted regression required; no continuation feature work authorized |
| Shared Composer deadline, timeout and cancellation | Request deadline owner; provider audit/custody settles the attempted call before propagation | Same absolute deadline; no fresh retry budget; no cancellation masking | Timeout/cancel audit stays distinct from unavailable/model-content failure | Focused mocked deadline/cancellation controls executed on inspected incident/current sources; no live latency diagnosis |
| Malformed provider frame/verdict | Admission boundary -> Advisor checkpoint bounded format retry -> completion owner | Existing bounded retry and publication binding | Malformed requires observed invalid content/frame; do not infer it from request rejection | Preserve existing response-evidence and malformed controls |
| First-party invariant/audit/settlement failure | Controlled-code owner, not recoverable provider outcome | Propagate to responsible operation handler; no success fallthrough | Existing private diagnostics and fixed public failure | Extend narrow call-path tests where handler reachability is proven |
| Required executor generation/admission unavailable | Shared executor/worker -> operation/API exception handler | Preserve 503/refusal; no new retry policy | Current handlers group these with `OperationalError` as database-unavailable, although origin can be executor admission | 0.8.10 follow-on: prove with focused handler tests, retain status and correct unsupported diagnosis |
| Composer observer application callback failure | UI stream callback -> application error owner | Transport/decoder fallback should remain; callback faults should not silently become transport fallback | Keep safe client diagnostics; do not replay a submitted operation | 0.8.10 follow-on: callback is currently inside stream catch-all; reproduce with a throwing callback |
| Composer observer owned kind mismatch | Bound operation snapshot -> owned protocol handler | Existing custody/backoff needed for real transport/GET faults; an owned mismatch needs an explicit disposition | Preserve mismatch identity rather than generic retry | 0.8.10 follow-on: source shows mismatch inside broad retry catch; test negative transport controls alongside it |
| Primary provider bad-request wrapper | Existing wrapper chains original SDK error; route translation is distinct from controlled Advisor completion | Current unchanged-request nonretryability | Preserve safe reason using an explicit descriptor; do not reuse raw debug-provider-detail path | 0.8.10 inspection item; not evidence that this path produced the urgent Advisor symptom |

## Source-grounded follow-on observations

- `src/elspeth/web/frontend/src/api/composerOperationObserver.ts`: the progress callback is invoked inside the stream catch-all; the operation-kind mismatch is thrown inside the polling catch/retry arm. Callback/owned-invariant failures need focused reproductions distinct from intentional transport and decoder recovery. Do not weaken custody or replay restrictions.
- `src/elspeth/web/sessions/composer_operation_errors.py` and `src/elspeth/web/app.py`: `RequiredGenerationUnavailable` and worker admission timeout can be labelled `database_unavailable`. Their defining origins include shared executor generation/admission. Preserve refusal/status while proving and correcting any unsupported diagnosis.
- `src/elspeth/web/composer/tool_batch.py`: a broad `TimeoutError` hint handler may also catch infrastructure admission subclasses. Reachability from quota/custody has not been established. Do not change it on class inheritance alone.
- The same dispatcher has a broad session-aware handler that can classify service failures as plugin crashes. Map actual registered handler routes and settlement responsibilities before treating every broad catch as a defect.

## Evidence gates and compatibility

For each repair, add a failing regression at the actual responsible handler plus negative controls for the deliberately recoverable neighbor. Verify original cause identity, safe serialization/rendering/reload, retry count and publication/cancellation behavior. Use focused affected checks and applicable whole-tree contract/attribute/masquerade/wire gates. Persistence changes require the repository's serial PostgreSQL checks before integration; a mocked SQLite result does not prove PostgreSQL behavior. Broad suites and release gates require separate resource admission.

Read historical payloads honestly: an absent historical descriptor means no reason was recorded, not proof the upstream omitted a reason. New closed cause/source vocabularies may be unreadable by an older binary even when additive fields preserve current readers; explicitly record that downgrade boundary. No migrations, resets or downgrades are authorized by this plan.

Keep urgent increment readiness separate from 0.8.10 coverage. Source inspection, isolated focused tests and independent review are distinct from integration, CI, signing, release qualification, deployment and live acceptance.
