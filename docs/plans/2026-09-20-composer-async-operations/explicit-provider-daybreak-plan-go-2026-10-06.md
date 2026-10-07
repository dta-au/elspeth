# Actual Daybreak provider/revocation plan GO — round 3

Actual model: `gpt-daybreak-blue-latest`, provider `openai`, native read-only CLI. Session `01a11204-78e5-7e51-aa83-5a39945d14cf`; completed exit `0`; **renewed narrow PLAN GO**.

All 28 immutable technical input hashes were verified after completion. B1–B6 are resolved at design level. This clears the required narrow design gate under the existing implementation workflow; it grants no source, executed-proof, frozen-tree, merge or deployment clearance. Both complete reviewed v3 proposals are preserved beside this verdict. All future controls below remain obligations. Complete raw review, native log and code snapshot remain in the task evidence workspace. Source line references identify that captured snapshot, not later mutable source. Only snapshot link prefixes and proposal-version filenames are normalized. Prior NO-GO verdicts and all proposals remain preserved.

# Renewed narrow PLAN review: GO

## Scope and method

Reviewed exclusively:

- [explicit-provider-custody-design-v3-2026-10-06.md](explicit-provider-custody-design-v3-2026-10-06.md)
- [revocation-source-split-design-v3-2026-10-06.md](revocation-source-split-design-v3-2026-10-06.md)

I reread both prior verdicts, the included normative companion designs, and the supplied current source snapshot as constraint and feasibility evidence. Current unwired source was not treated as accepted final implementation.

The installed plan-review skill ordinarily delegates reality, architecture, quality, and systems reviews and writes a report. Because this review prohibits nested models and source writes, I performed all four lenses directly. No tests, network, history, archives, runtime data, provider calls, deployments, or source changes were used.

## Verdict

**Actual renewed narrow PLAN GO.**

The repaired v3 proposals resolve the sole remaining B6. No new contrary normative or current-source constraint was found.

This approves the plan architecture for the concrete explicit-provider-custody restructuring and source-22/26/27 split. It does not provide implementation clearance, executed-proof clearance, final source clearance, merge approval, deployment approval, or provider authorization.

## Prior blocker disposition

| Blocker | Disposition | Review basis |
|---|---|---|
| B1 — terminal audit with `recorder=None` | **Resolved** | Required mode always constructs and retains the terminal `ComposerLLMCall` after an admitted dispatch. Buffering is optional and independent. Admission refusal remains the sole no-call arm. Audit-body failure leaves the admitted attempt unknown; it cannot synthesize zero usage or source 9. |
| B2 — TURN/TITLE allocation | **Resolved** | Closed table is exact: TURN uses 7/8, shared 9 only when positively undispatched, and 10/11; TITLE uses 44/45, shared 9 only when positively undispatched, and 46 only. No projections are invented for 9 or 46. This matches the registry in [required_work.py:70](../../../src/elspeth/web/required_work.py:70). |
| B3 — four-family mint/recurrence ownership | **Resolved** | One `ProviderInvocationOwner` holds independent PRIMARY, ADVISOR, PLANNER, and TITLE counters. The table selects each sole mint point, caller path, role, ordinal source, recurrence owner, retry boundary, scope owner, and ticket completer. Lower-layer reconstruction is prohibited. |
| B4 — source-22 result under cancellation | **Resolved** | `run_required_sql_finish_once` exposes a nominal actual-return/original-error union with identity-preserved deferred cancellations. The service consumes the actual source-22 result before cancellation may escape and never uses a private ticket-result getter or database reread to infer eligibility. |
| B5 — source-26 result/source-27 decoder/completion | **Resolved** | The plan selects `ComposerRevocationSQLResult`, immutable expected evidence, a strict source-27 decoder, exact actual-event return, original error preservation, and an exhaustive unused/actual/unknown ownership table. |
| B6 — source-23 closed disposition and exact unused ABI | **Resolved** | Source 23 now has its own closed three-value vocabulary, coordinator-issued provenance metadata, an explicit field on every non-business service arm, and the exact `ticket.complete_unused(metadata)` capability. There is no free disposition override, exception-derived classification, generic unknown completion, or synthetic failure. |

## B6 independent resolution

The repair is closed and deterministic.

### Closed source-23 vocabulary

`PublicationProjectionDisposition` contains exactly:

- `PREFLIGHT_REFUSED`
- `PUBLICATION_FAILED`
- `ELIGIBILITY_SELECTED`

Its selection depends solely on the actual source-22 physical branch:

| Actual source-22 branch | Source-23 disposition |
|---|---|
| Proven refusal before physical submission | `PREFLIGHT_REFUSED` |
| Known submitted source-22 failure before eligibility | `PUBLICATION_FAILED` |
| Actual returned revocation eligibility | `ELIGIBILITY_SELECTED` |
| Actual business result | Mandatory source-23 projection; no unused metadata |
| Submitted-but-unknown source 22 | No service arm and no source-23 completion |

Once eligibility is selected, the disposition remains `ELIGIBILITY_SELECTED` across source-26 pre-submit refusal, source-26 SQL failure, source-27 validation failure, independent post-completion fault groups, and successful validated revocation. Thus the same exception object appearing in different branches cannot determine or alter the disposition.

### Selected metadata issuer

The exact issuer is:

```python
issue_publication_projection_unused(
    *,
    publication_ticket: RequiredWorkTicket,
    projection_ticket: RequiredWorkTicket,
    actual_outcome: RequiredSQLFinishOnce[PipelinePublicationSQLResult],
) -> PublicationProjectionUnusedMetadata | None
```

This is sufficient because the coordinator retains:

- the registered source-22 and source-23 ticket identities;
- whether source 22 received a bound physical Future;
- actual observed Future outcome or proven no-submission state;
- the precise source/scope/authority relationship;
- the exact issued metadata object identity.

Consequently, a `RequiredSQLRaised` arm need not expose a caller-controlled reason: the coordinator distinguishes proven pre-submit refusal from a submitted known failure using its retained physical custody, not the exception’s class, message, cause, or receipt category.

### Exact completer ABI

The selected ABI is:

```python
ticket.complete_unused(
    metadata: PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata
) -> None
```

It is appropriately capability-based:

- `reserve()` installs the retaining coordinator before exposing the ticket.
- Only privately issued metadata object identity is accepted.
- Same-value reconstructed metadata is insufficient.
- Namespace, target key, source, scope and originating physical provenance are validated.
- A bound Future, unknown submission, already completed ticket, or started projection refuses.
- `begin_projection()` prevents “no Future” from being misread as unused projection proof.
- The operation records unused completion without inventing an exception or category witness.
- Actual pre-submit failure continues through the separate real-error completion path.

The service union also supplies everything the sole source-23 caller needs:

- Business arm: mandatory nominal source-23 projection and no unused metadata.
- Revocation-completed arm: literal `ELIGIBILITY_SELECTED` plus exact issued metadata.
- Raised arm: explicit disposition plus matching exact issued metadata.
- Unknown source-22/source-26 physical custody: no arm, hence no possible source-23 completion.

B6 is therefore fully repaired.

## Contrary-constraint review

### Provider admission and custody

The current ContextVar `_SCOPE`/`_SPAN` design confirms the need for explicit required custody; it does not prohibit the proposed split. Existing admission is immediately before the physical LiteLLM entry in [provider_gateway.py:421](../../../src/elspeth/web/composer/provider_gateway.py:421).

The proposal preserves that ordering:

1. Validate credentials/request/model and explicit custody.
2. Perform actual chargeable admission.
3. Obtain the real provider attempt.
4. Signal physical dispatch.
5. Enter the SDK.

No provider/vendor kwargs carry custody authority.

Required mode and legacy mode are disjoint:

- A valid explicit owner/binding selects required custody.
- Null binding retains the existing legacy decorator/ContextVar behavior.
- Required mode never enters the legacy provider span.
- Supplying only one of planner service/owner refuses before launch.
- EXECUTE, direct, test and unscoped compatibility paths cannot manufacture required receipts.
- Manual PROPOSAL authority remains publication-only and gains no provider authority.

### Attempt/audit/usage bijection

The proposed lifecycle is coherent:

- Admission refusal: one admission callable, zero SDK dispatches, no fabricated terminal call.
- Admitted and positively undispatched: source 9 only, with a real attempt and no-SDK-entry witness.
- Dispatched: one complete terminal call is retained regardless of recorder presence.
- Settlement requires both the actual admitted attempt and final audit.
- Retry N+1 cannot admit until retry N’s terminal audit and settlement finish.
- Audit construction failure after dispatch preserves an unknown admitted attempt and original failure rather than creating zero usage.
- Refusal-recorder work remains inside the registered admission callable; no second unregistered Future is created.

The present `recorder is not None` guard at [provider_gateway.py:654](../../../src/elspeth/web/composer/provider_gateway.py:654) is an implementation seam the plan explicitly changes, not a contrary constraint.

### Direct child cancellation and late generation failure

Current required-work custody distinguishes actual Future completion, aborted invocation proof, submission-unknown state, and generation recovery. See [required_executor.py:102](../../../src/elspeth/web/required_executor.py:102) and [required_work.py:403](../../../src/elspeth/web/required_work.py:403).

The proposals correctly require:

- shielded ownership and joining of admission/source-22/source-26 work;
- no inference from a canceled awaiter;
- no source 9 without an actual attempt and positive no-SDK-entry witness;
- no secondary source-26 launch from unknown source-22 custody;
- no unused disposition for submitted-but-unknown source 22 or 26;
- no terminal result, proposal-child completion, lease release or generation replacement before actual physical custody closes.

The new finish-once ABI repairs the current bridge behavior that otherwise discards a completed result when cancellation was observed.

### Source 22 versus sources 26/27

The source registry assigns:

- 22/23 to pipeline publication;
- 26/27 to revocation audit.

The v3 proposal respects that division.

Source 22:

- performs locked publication eligibility checks;
- returns either the nominal business result or private `_ComposerRevocationRequired`;
- performs no revocation DML on the eligibility branch.

Source 26:

- independently restores and checks actor, user, job, attempt, SOL identity and epoch, tokens, claim, fence, lease, proposal, v3 creation binding, dispatch, tool, arguments, content hashes and current trust;
- may cross Stop/deadline only for the sealed forensic operation;
- refuses stale, foreign, released, expired, lost or mismatched authority;
- exposes only one exact revocation insert or immutable replay.

Source 27:

- strictly parses the closed `auto_commit.revoked.v2` evidence;
- rejects extra/missing/wrongly typed fields and noncanonical owned IDs;
- compares every event and payload identity against immutable expected evidence;
- returns the same physical event, preserving its ID, timestamp and payload;
- performs no corrective write;
- records a decoder failure only at source 27.

This is compatible with the existing revocation helper seam beginning around [service.py:3416](../../../src/elspeth/web/sessions/service.py:3416).

### Stop, deadline and reducer behavior

No conflict was found with the normative collision reducer:

- Successful revocation is forensic evidence, never business success.
- Stop/deadline remains the durable operation result after required revocation work completes.
- Audit-integrity and SQL categories retain their existing priority.
- Arbitrary SDK errors are not promoted through cause inspection.
- Source-26 failure remains source 26 and creates no source-27 error.
- Source-27 failure after successful source 26 retains both the real event and original decoder failure.
- Original SQL, projection, body, cancellation and SDK objects remain available.
- Receipt and exception-group arrival order cannot choose the public result.

## Four-lens assessment

### Reality — PASS

All required current seams exist: task-local quota spans, physical pre-SDK admission, optional recorder behavior, planner call ordinals, title task ownership, required Future custody, source registry, publication transaction, revocation helper and canonical reducer.

The selected types and methods are new plan APIs, but no supplied source constraint makes them unimplementable.

### Architecture — PASS

The design has clear one-owner boundaries:

- `ProviderInvocationOwner` mints stable invocation identity.
- `ProviderCallCustody` owns one logical invocation and its physical retries.
- The required executor owns physical Future completion.
- The settlement service owns 22/26/27.
- The caller owns source 23.
- The coordinator alone issues unused-completion capabilities.
- Source-22 eligibility is evidence, not transferable write authority.

No concurrency authority bleed or lower-layer identity reconstruction remains in the selected design.

### Quality — PASS, proofs mandatory

The proposals now specify error preservation, recorder absence, audit construction failure, refusal-recorder failure, retry ordering, cancellation, projection validation, replay identity, unused-versus-unknown completion and deterministic reduction strongly enough for implementation.

The proof suite below remains mandatory; its absence would block later source clearance, not this plan verdict.

### Systems — PASS

The design prevents the significant systemic failures:

- chargeable provider dispatch before admission;
- duplicate or scheduler-dependent invocation identity;
- settlement before final audit;
- reuse of one custody concurrently;
- source-22 eligibility granting source-26 mutation authority;
- marking unknown physical work unused;
- releasing the proposal child or lease while required work remains unknown;
- successful revocation suppressing Stop/deadline;
- reducer results changing with completion order.

## Required future controls

### Positive integration proofs

1. PRIMARY, ADVISOR, PLANNER and TITLE obtain distinct custody from their sole family mint sites.
2. Reversing advisor/title completion order does not change semantic identities.
3. Each physical retry gets a fresh recurrence; retry N fully settles before admission N+1.
4. Required `recorder=None` retains one complete terminal call and measured usage while appending nothing to a buffer.
5. TURN uses exactly 7/8, shared 9 only when positively undispatched, and 10/11 when dispatched.
6. TITLE uses exactly 44/45, shared 9 only when positively undispatched, and 46 only when dispatched.
7. Quota refusal runs inside the one registered admission callable, preserves the original refusal, and dispatches zero SDK calls.
8. Source-22 business completion under repeated cancellation validates/completes 23 before original cancellation escapes.
9. Source-22 eligibility under repeated cancellation completes 26, then validates/completes 27, then completes unused 23 before original cancellation escapes.
10. Source 26 independently rechecks every actor/job/claim/lease/fence/dispatch/hash/trust predicate.
11. Source 27 returns the exact source-26 event after closed-schema validation.
12. Every non-business service arm carries the exact source-23 disposition and privately issued metadata.
13. Normal business publication completes 26/27 with genuine `BUSINESS_SELECTED` unused metadata.
14. Stop/deadline plus successful revocation retains a failed operation result and performs no business write.
15. Groups preserve every original SQL, projection, cancellation, body and SDK object while reduction remains order-independent.

### Negative integration proofs

1. Missing, foreign, stale, wrong-role, wrong-context, duplicate or impostor owner/custody refuses before SDK dispatch.
2. Required paths never obtain authority from `_SCOPE`/`_SPAN`; adversarial ContextVar changes have no effect.
3. Null-binding legacy Composer and EXECUTE paths preserve existing behavior and create no required tickets.
4. Planner service without owner, or owner without service, refuses before lifecycle/provider launch.
5. Manual PROPOSAL settlement invokes no provider and cannot create COMPOSE custody.
6. Concurrent reuse of one custody refuses before dispatch.
7. Audit construction failure after dispatch cannot invoke source 9 or invent zero usage.
8. A directly canceled admission child without its actual returned attempt cannot invoke source 9.
9. Submitted-unknown source 22 completes none of 23/26/27.
10. Submitted-unknown source 26 completes neither 23 nor 27 and blocks terminal/lease release.
11. Forged, copied, cross-operation, wrong-source, wrong-key, wrong-scope or wrong-namespace unused metadata refuses.
12. A bound Future, started projection or duplicate completion cannot be marked unused.
13. Business results cannot carry source-23 unused metadata.
14. Forged, stale, unbound-v2, wrong-actor/job/claim/lease/fence/dispatch/hash/trust eligibility performs no source-26 DML.
15. Source-26 failure creates no source-27 event or fabricated decoder failure.
16. Successful source 26 followed by source-27 failure cannot become revocation success.
17. No caller or service may derive source-23 disposition from exception type, text, cause, group ordering or receipt category.
18. Successful revocation cannot suppress Stop, deadline, integrity failure or original cancellation.

### Mutation proofs

Tests must fail if a mutation:

- re-enters the legacy provider span in required mode;
- reconstructs invocation identity below the owner;
- moves admission after SDK entry;
- forwards custody/coordinator through vendor kwargs;
- drops terminal audit when `recorder=None`;
- admits retry N+1 before settling N;
- reuses a recurrence across retries;
- moves refusal recording to a second Future;
- assigns TITLE source 11 or invents a projection for 9 or 46;
- uses source 9 after SDK dispatch;
- infers admission from a database reread;
- lets source 22 insert revocation evidence;
- skips any source-26 authority recheck;
- omits or weakens source-27 decoding;
- replaces the actual replay event ID, timestamp or payload;
- issues metadata with a free caller-supplied disposition;
- accepts copied same-value metadata;
- marks submitted-unknown work unused;
- completes source 23 without union-carried metadata;
- changes `ELIGIBILITY_SELECTED` after a downstream failure;
- releases proposal-child, generation or lease custody before physical completion;
- changes reducer output with receipt/group arrival order;
- loses or replaces an original exception or cancellation object.

## Clearance boundary

The earlier broad plan GO remains intact, and this previously held narrow restructuring is now **PLAN GO**.

Implementation must still satisfy every retained B1–B6 obligation and the controls above. This review supplies no executed-proof or final source clearance.

Skills materially used: `plan-review`, `using-software-engineering`.
