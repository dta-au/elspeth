# Actual Daybreak Blue final plan verdict — 2026-10-06

Requested model `gpt-daybreak-blue-latest`, Codex CLI 0.160.0, existing ChatGPT login, ephemeral read-only sandbox, ignored user configuration, no inherited shell environment. Native CLI exit 0; thread `01a110a1-d81d-7e60-b5a6-5dac684c637e`. Complete native response, JSONL, stderr, explicit exit, unchanged authorization transcript and all three frozen review payloads remain preserved in the consumer workspace. This durable copy changes only machine-specific link targets/home references.

The actual review returned GO for held-branch implementation and closed B1–B5; it is plan approval only. Application implementation was not performed in this readiness phase. The two original reports remain missing and the separate archive-diff denial remains unresolved. Neither custody nor authorization is inferred from the verdict.

Execution trace: 23 completed read commands, all `/usr/bin/bash -c` (non-login). One initial search used an incorrect index path and exited 2; subsequent direct reads corrected that path. Native CLI stderr is empty. Installed mandatory workflow instructions were additional tooling reads, disclosed below. No successful independent fork/subreview is claimed. Prior rounds and their operational limitations are retained unchanged.

---

# Daybreak Blue third focused plan review: GO

**GO for held-branch implementation.** The round-two blockers B1–B3 are corrected in the active index, contract, and task pages. B4 and B5 remain closed. I found no new active contradiction or unassigned executable requirement.

This is plan approval only. It is not code approval, merge/deployment authority, production/provider authorization, or evidence that future tests already pass.

## B1–B5 verdicts

### B1 — RESOLVED

The active contract now defines one consistent retry candidate:

- Full ordered transcript and existing conversational filter.
- Latest conversational user must match `expected_user_message_id`.
- An eligible partial assistant has **nonempty `tool_calls`**, with narration optional.
- A bare assistant without tool calls—including narration-only—is a saved reply and is refused as `recompose_already_completed`.
- Exact same-user pending/committed proposals are refused; rejected proposals remain eligible.
- The original user precedes the retained prefix.
- No duplicate user and no stored tool/result replay.
- No tail-role refusal survives.

Anchors: [contract.md:357](contract.md#L357), [contract.md:621](contract.md#L621), [N06.md:11](N06.md#L11), [N06.md:32](N06.md#L32).

This matches the exact recovered candidate: its guard rejects any later assistant without tool calls and permits tool-call prefixes. Its proof matrix covers tool-only and narration-plus-tool-call prefixes, while settled prose is refused. The former narration-only eligibility contradiction is gone.

The active index’s phrase “partial tool prefixes” is consistent. References to current main’s obsolete tail-role behavior describe measured preimplementation reality, not active semantics.

### B2 — RESOLVED

The recovery arms are now disjoint and close the late-commit race:

1. Exact immutable-bundle retry requires:

   - Actual completion of the original SQL/transaction.
   - Fresh committed proof from a new writer-database transaction/connection.
   - No terminal.
   - Exact live fence.
   - Unchanged terminal-driving state.
   - A locked recheck before writing.
   - At most one retry.

2. If state changes during the independent read or locked recheck, that arm rolls back without writing.

3. Changed-state reconciliation is a separate, failure-only arm. It retains the original attempted bundle and requires all original and attempted-retry SQL to have actually finished, another fresh writer-database nonterminal proof, the live fence, and a locked priority recheck. It permits one bounded failed-terminal CAS only.

4. Lost fence, unreadable/unknown state, possible outstanding SQL, or corrupt/foreign terminal never competes.

Anchors: [contract.md:389](contract.md#L389), especially [contract.md:407](contract.md#L407)–413, and [N07.md:32](N07.md#L32).

A late original or retry commit cannot compete because a new attempt is forbidden until the relevant SQL has actually completed and a fresh writer read proves no terminal. A state change during the locked retry recheck cannot leak through: that retry rolls back and the failure arm must independently reacquire all prerequisites. The closed priority table preserves committed terminals and higher-priority original integrity/accounting evidence.

### B3 — RESOLVED

Backend ownership is now explicit:

- N13 owns `ComposerStreamAuthServices`, issuer verifier wiring, selected local/SSO provider wiring, app state, operations route, and bounded ASGI response.
- N15 owns frontend bearer headers, decoder, observation, and custody only.
- The facet exposes an existing `SessionTokenIssuer.decode` callable and owned claims—not signing material.
- No request-state claims, copied key, new credential/grant, or structural Protocol dispatch is authorized.

Anchors: [contract.md:644](contract.md#L644), [N13.md:37](N13.md#L37), [N15.md:43](N15.md#L43).

This is implementable against current factory source:

- Local construction creates the issuer immediately before the provider at [app.py:1251](../../../src/elspeth/web/app.py#L1251).
- SSO construction retains `sso_wiring.token_issuer` and selects the matching provider at [app.py:1704](../../../src/elspeth/web/app.py#L1704).
- The selected provider is published at [app.py:1711](../../../src/elspeth/web/app.py#L1711)–1726.
- `decode` verifies signature, issuer, audience, provider and expiry and returns owned claims at [session_token.py:205](../../../src/elspeth/web/auth/session_token.py#L205).

The race statement is honest: authorization and socket delivery are not globally atomic; one already-authorized, already-initiated bounded frame can race revocation. No new frame may start after denial. The serial worst-case detection bound is stated as send ≤5 seconds + cadence ≤1 second + check ≤2 seconds, and must be measured rather than assumed.

### B4 — REMAINS RESOLVED

N17 retains the controlled PostgreSQL RESTRICT/NO ACTION comparison and separates it from final-schema proofs. Supplied current-main evidence records raw DDL, delete SQL, reflected/catalog actions, successful cascades, and real `23503` restrictive controls for both variants. It correctly says this is a semantics control, not historical-defect reproduction.

Anchor: [N17.md:35](N17.md#L35).

### B5 — REMAINS RESOLVED

The active implementation baseline is consistently `23822a7477624d6264c60fade0905c19e8c552d7`. Historical `edc844…` evidence and the recovered candidate are not represented as current implementation state. PR276 remains separately owned, and the plan requires a refresh after it lands.

## Historical and custody assessment

The historical map retains:

- 97 labelled prior-obligation rows at lines 9–105.
- Five semantic/Appendix rows at lines 106–110.
- Links to the complete original pages and their full cells.
- Active destinations and future proof obligations without claiming historical implementation status.

Anchor: [historical-obligation-map-2026-10-06.md:9](historical-obligation-map-2026-10-06.md#L9). Its recorded instrument includes a known-positive table, fenced-code negative, and inserted-row mutation control.

I found **no actual unassigned executable requirement**. Future proof work remains open by design; that is not the same as missing assignment.

The following original reports remain unavailable:

- `2026-10-06-frontend-streaming-readiness.md`
- `2026-10-06-frontend-streaming-source-review.md`

I do not claim custody, sentence-level recovery, absence of unknown observations, or knowledge of their unavailable contents. Their absence remains an evidentiary limitation, but not a material safety blocker: the recovered packet, full historical pages, active contract/tasks, retry candidate, current source, transport/auth/frontend/schema obligations, and obligation map cover every identifiable safety domain. If recovered later, they must be reconciled against the map.

The unresolved archive-diff denial remains recorded. No archive diff was read or authorized.

## Mandatory implementation gates

Implementation may start only on the held branch. Before any completion claim:

- Refresh affected provenance after PR276 lands.
- Prove exact retry eligibility and all four real-handler refusal envelopes, including pagination failure/nonadvancement, tool-only and narrated-tool prefixes, narration-only refusal, no duplicate user, and no stored tool/result replay.
- Fault-inject terminal SQL before commit, during commit, and after server commit/before acknowledgement.
- Prove delayed original/retry SQL completion, failed independent reads, changes between read and locked recheck, cancel/deadline against every priority arm, no competing terminal, and no provider replay.
- Run SQLite and serial PostgreSQL schema, contention, trigger, archive-order, nullability, malicious cross-session, deletion, fencing, and terminal proofs.
- Prove local and SSO issuer/provider/principal agreement, live role/identity/ownership/archive revocation, expiry boundary, immediate pre-send recheck, and suppression of every uninitiated frame after denial.
- Measure the 1-second cadence, 2-second auth/read deadline, 5-second actual ASGI-send bound, and honest 8-second worst-case subsequent-detection bound. Cancellation-resistant send/SQL controls must retain permits until underlying completion.
- Prove frontend decoder fragmentation/size failures, custody bounds, reconnect/poll fallback, logout/principal replacement, two-tab and ambiguous-admission behavior, Stop, and terminal-versus-cancel handling.
- Run actual local TLS with a delayed fake provider and observe an early progress or heartbeat frame—not merely response headers—before durable completion.
- Preserve provider-authored proposals and ordinary tutorial/rootless paths, with provider calls checked per transition.
- Run applicable focused, whole-tree, default, and serial testcontainer gates with recorded raw logs and exit codes.
- Obtain independent Astra and Daybreak implementation/code reviews.
- Keep the completed implementation under **John’s local-testing hold**.

No merge, deployment, production access, paid-provider calls, provider answer-token streaming, or ready-for-merge assertion is authorized. Product scope remains **live provider-safe progress followed by the completed durable answer**.

## Review boundary

I performed no writes, implementation, tests, provider/production/network calls, secret or environment inspection, or archive/PR276-diff access.

I additionally read two installed tooling-context files outside the supplied project tree:

- `<operator-home>/.agents/skills/superpowers/using-superpowers/SKILL.md`
- `<operator-home>/.codex/skills/plan-review/SKILL.md`

Those were mandatory workflow instructions, not ELSPETH evidence or additional private-source authorization. Every shell invocation used `login:false` and absolute utility paths; no profile/startup file was sourced.

**Final verdict: GO for held-branch implementation, subject to all future gates and John’s local-testing hold.**
