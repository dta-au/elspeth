# Actual Daybreak Blue first review — 2026-10-06

Requested model: `gpt-daybreak-blue-latest`; installed Codex CLI 0.160.0, existing ChatGPT login, read-only sandbox, ignored user config, ephemeral session, shell environment inheritance disabled. CLI exit 0; thread `01a11086-9ae4-7ce2-88f5-f69f8a13030f`. Native complete review and execution JSONL remain in the consumer workspace; this durable copy normalizes only machine-specific source links.

The reviewer returned NO-GO with B1–B5 and explicitly assessed the two missing reports as an evidentiary limit rather than an independently material safety blocker. This is not original-report custody or an implementation verdict. Corrections and a focused actual Daybreak re-review follow; the first verdict remains retained.

Operational scope correction: the native execution trace shows the CLI's mandatory workflow read two installed instruction files (`using-superpowers` and `plan-review`) before reading the bounded input. Thus the review's final statement that it read only the input directory is too broad. No credential/auth/config/environment-value or PR276/archive-diff read appears in the trace. The private ELSPETH source/plan payload stayed within the bounded copied input; routine installed workflow instructions were additional tooling context. No application/provider tests ran in the reviewer.

---

# Daybreak Blue plan review: NO-GO

Implementation should not start yet.

The overall architecture is strong and substantially closes the recovered twelve-finding safety coverage, but four contract defects remain too ambiguous or internally inconsistent for independent workers to implement safely. A fifth documentation correction is required to prevent work against the wrong source baseline.

This is a plan verdict, not an implementation verdict. The proposed code and future tests do not exist, and I do not treat that fact alone as a blocker.

## Severity-ranked blocking findings

### B1 — Critical: recompose eligibility is internally contradictory

Measured current behavior rejects recompose when the last conversational row is not a user row. The recovered candidate deliberately changes that rule: find the latest user, reject only a later completed assistant reply or exact saved proposal, and allow a partial narration/tool-call prefix without replaying stored tools.

The active task pages state the recovered semantics correctly, but the controlling interface contract still specifies:

> “last row not user → today’s 409 string”

at [contract.md:357](contract.md#L357). That contradicts E5’s reference to latest-user/candidate semantics and the explicit N06/N10/N11b/N14/N15 obligations.

Current source confirms that the old tail-role check remains in place at [compose.py:138](../../../src/elspeth/web/sessions/routes/composer/compose.py#L138). Consequently, an implementer following the contract literally can reintroduce the very behavior the retry candidate is intended to replace.

Minimal correction:

- Replace the stale refusal list in the composite-start contract with one exact algorithm:
  1. Load the full ordered history.
  2. Apply the existing conversational filter.
  3. Find the latest conversational user row.
  4. Require that row’s ID to equal `expected_user_message_id`.
  5. Refuse a later assistant row without tool calls as `recompose_already_completed`.
  6. Refuse an exact same-user pending or committed proposal as `recompose_saved_proposal`.
  7. Do not let rejected proposals block a fresh explicit attempt.
  8. Permit a partial narration/tool prefix.
  9. Construct provider history from the original user and safe prefix without duplicating the user or replaying stored tools.

Required proofs:

- Full filtered history, including pagination/non-advancing-page refusal.
- Latest-user mismatch.
- Saved reply and pending/committed proposal refusal.
- Rejected proposal remains eligible.
- Partial narration-only, tool-call-only and mixed prefixes remain eligible.
- No duplicate user row.
- No stored tool execution/result replay.
- Exact four candidate public refusal envelopes through the real handler projection.

### B2 — Critical: terminal priority and unknown-commit recovery are not executable contracts

The plan recognizes cancellation, provider settlement, quota/usage, audit persistence, auto-title, lease loss and terminal commit as competing outcomes. It does not define a complete precedence table.

Appendix A assigns “cancellation/settlement priority” to N07/N09/N10/N11b and says integrity/storage failure must not be hidden by cancellation or provider weather, but the active contract never states the exact ordering. The worker description therefore does not determine what to persist when, for example:

- explicit Stop races a provider-settlement failure;
- audit or token-usage persistence fails while cancellation is pending;
- auto-title cancellation exposes a charge-settlement failure;
- terminal commit succeeds but the database driver reports an ambiguous failure;
- terminal commit fails and SOL close or renewal then fails.

The composite terminal at [contract.md:364](contract.md#L364) covers a normal transaction, and E12 covers auto-title joining, but neither defines commit-unknown reconciliation. Retrying a failure terminal after an unknown completed commit must not relabel or obscure the completed result.

Minimal correction:

- Add a closed terminal-priority table covering:
  - already committed terminal;
  - authoritative audit/integrity/storage failure;
  - provider/owned-child settlement failure;
  - explicit cancellation;
  - deadline;
  - lease loss/shutdown;
  - generic defect.
- State that a committed, hash-valid terminal always wins every later exception, cancel and lease-close failure.
- After any commit-unknown exception, perform an independent committed read:
  - valid terminal → return that terminal;
  - still-running and fence owned → bounded retry of the same terminal composite;
  - fence lost/nonterminal ambiguity → leave for the reaper without a competing terminal;
  - corrupt terminal → `AuditIntegrityError`, never a replacement result.
- Name every owned child that must be joined before terminal settlement: provider-attempt/quota accounting, token usage and audit cohort, proposal/response projection, auto-title, and any settlement child.
- Clarify that proposal/state “atomicity” means the terminal response is built from a same-transaction committed snapshot. If proposal/state mutations intentionally commit during tool execution, say so honestly and define crash-visible partial state rather than implying those earlier mutations are part of the terminal transaction.

Required proofs:

- Fault immediately before commit, during commit, and after server-side commit/before acknowledgment.
- Explicit cancel against every failure-priority arm.
- Provider quota and token-usage fault/timeout tests.
- Auto-title success, timeout, cancellation and charge-settlement failure.
- No second terminal and no post-terminal non-audit write.
- Hash-valid completed terminal survives late cancel, lease-close `ExceptionGroup` and worker shutdown.

### B3 — High: authorization-to-send TOCTOU guarantee remains deliberately unresolved

The plan correctly requires:

- Bearer authentication;
- live role, identity, provider and ownership checks;
- bounded reauthorization;
- token-expiry closure;
- no emission after a failed check;
- an actual ASGI send timeout;
- no credentials or unsafe provider/tool/reasoning material in frames.

Current source authenticates a Bearer token and separately checks the live human role at request entry in [middleware.py:80](../../../src/elspeth/web/auth/middleware.py#L80) and [middleware.py:168](../../../src/elspeth/web/auth/middleware.py#L168). There is no existing long-lived authorization guarantee to inherit.

The contract itself acknowledges that source snapshots, authorization changes and socket sends cannot be globally atomic, then leaves Daybreak to “assess the precise guarantee” at [contract.md:784](contract.md#L784). That assessment must become contract text before implementation.

Minimal correction:

- Define the achievable guarantee explicitly:
  - authorization is checked from committed live state before each data frame;
  - the frame is discarded if its check fails;
  - authorization and socket delivery are not globally atomic;
  - a revocation committed after the final successful check may race one already-authorized send;
  - the maximum detection interval is the smaller of the read cadence and the next attempted frame/heartbeat;
  - token expiry is checked against the server/database clock and forbids initiating a send at or after expiry.
- Do not promise “zero bytes after revocation commit” unless the design introduces a transactional revocation generation that is checked at the actual send boundary and can genuinely provide that property.
- Specify how a frame prepared before reauthorization is invalidated: recheck immediately before the timed ASGI `send`, and do not reuse the result across frames.
- Define whether provider reauthentication occurs each time or verified token claims plus live database authority are sufficient for each supported provider.
- State post-header failure behavior: clean EOF or one fixed redacted control frame, followed by no further data.

Required proofs:

- Revocation before frame construction, between construction and recheck, and while ASGI send is blocked.
- Token expiry at the boundary.
- Principal/provider replacement and archived/ownership-changed session.
- Pending frame suppressed after failed recheck.
- No raw exception, token, prompt, tool argument/result, reasoning or credential material in frames and logs.
- Effective five-second timeout around the ASGI send call itself, not only generator production.

### B4 — High: final schema proof omits the required controlled RESTRICT/NO ACTION comparison

E19 proposes a defensible FK layout, but the evidence plan only calls for an N00 PostgreSQL cascade with a RESTRICT sibling and a later proof of the final set. N17 does not explicitly require the minimal sibling probe for both `RESTRICT` and `NO ACTION`.

That is insufficient because the plan’s justification depends on statement-end behavior and cascade ordering. The probe must characterize both actions without claiming that it reproduces the historical failure hypothesis.

Minimal correction:

- Add a controlled two-variant PostgreSQL probe:
  - identical parent/cascaded child/sibling schema;
  - variant A uses `RESTRICT`;
  - variant B uses `NO ACTION`;
  - record raw DDL, delete statement, result and reflected FK action.
- Label this a semantics/control probe only. Do not claim it reproduces the old defect.
- Keep final composite schema proofs separate and mandatory on SQLite and PostgreSQL.
- Add explicit final-schema deletion cases for:
  - job→session cascade;
  - actor identity RESTRICT;
  - user-message and base-state NO ACTION;
  - ingress→job composite cascade;
  - live-session delete trigger;
  - archive/session deletion order;
  - nullable composite members and malicious cross-session bindings.
- Add those obligations directly to [N17.md](N17.md), not only to historical notes.

### B5 — Medium: the authoritative baseline is contradictory

The contract opens by saying it is against current main `23822a7477624d6264c60fade0905c19e8c552d7`, but its Tree clause still says main was verified at `edc844699a350a90a624089e12a5a1e75b7b2dde` at [contract.md:15](contract.md#L15).

The reconciliation document correctly distinguishes current main from historical evidence, but the interface contract is supposed to be the implementation authority. Leaving both hashes in its source-baseline declaration makes “live symbol” and measurement instructions ambiguous.

Minimal correction:

- Set the contract’s implementation baseline to `23822a7477624d6264c60fade0905c19e8c552d7`.
- Label every `edc844…` inventory, Appendix A result and measurement explicitly historical.
- Require N00 to repeat only affected current-main inventories rather than relabelling old outputs.
- Refresh again after PR276 lands or record a coordinated integration base. PR276 remains independently owned and its absent archive diff remains out of scope.

## Recovered twelve-finding coverage matrix

| Recovered finding | Active coverage | Assessment |
|---|---|---|
| 1. Async operations not implemented | Durable 202 jobs, operation GET, worker/reaper and terminal replay across N03–N14 | Covered as proposed design; implementation proof remains future work |
| 2. Disconnect currently cancels work | Worker owns turn; subscription disconnect owns only reader resources; Stop uses cancel endpoint | Substantively covered |
| 3. Operation/progress identity insufficient | Operation ID plus exact CRL/worker-lease binding; repeated recompose test | Substantively covered |
| 4. Entry-only authentication | Bearer fetch SSE, recurring live role/identity/ownership/provider checks | Covered in scope, but B3 blocks on the exact TOCTOU guarantee |
| 5. Safe progress vs answer streaming | Product choice explicitly safe progress followed by durable completed answer; gateway remains complete-response | Closed correctly |
| 6. Transport readiness unproven | Real local TLS, delayed fake provider, early chunk, buffering and idle fallback | Covered by required future acceptance |
| 7. Effective backpressure bounds | Actual ASGI send timeout, caps, coalescing, frame and lifetime bounds | Substantively covered; values remain conditional on measurement |
| 8. PostgreSQL/SQLite durability difference | PG durable cross-instance progress; SQLite may lose advisory progress but retains terminal | Covered honestly |
| 9. Full-history/recompose guards | Candidate semantics repeated through N06/N10/N11b/N14/N15 | Intended coverage is strong, but B1’s stale contract clause is blocking |
| 10. Checks before persistence | Credential/compartment, ownership and strict DTO checks precede durable request storage | Covered; implementation must prove no refused body reaches `request_json` |
| 11. Principal-scoped browser custody | Submitted/observer sum type, exact body, principal/provider scope, logout barrier and bounded retention | Covered in detail |
| 12. Schema reconciliation | Re-read epoch pair, both dialects, triggers/FKs/indexes and serial PG | Mostly covered; B4 adds the missing controlled sibling proof |

The recovered packet therefore provides broad substantive safety coverage. It is not merely a summary of headings: the active contract assigns identities, races, bounds, failure behavior and test targets. Its remaining gaps are concrete and repairable rather than evidence that the entire plan must be reconstructed.

## Prior detailed task safety obligations

The historical N00/N01/N05/N06/N07/N11a/N11b/N12/N13/N14/N15/N16 analyses remain useful and materially detailed. The active pages preserve them by reference and generally carry their important themes forward:

- positive mutation fences and audit-only classification;
- claim/start/adopt ordering;
- no running→queued transition;
- worker-lifetime capacity accounting;
- owned-child joining;
- durable completion winning deferred cancellation;
- real handler parity;
- tamper controls;
- lock ordering and PostgreSQL serial proofs;
- tutorial/provider-call invariants.

They are not fully “discharged,” because the active bundle does not contain a finding-by-finding closure ledger. That is not by itself a reason to forbid implementation: future-code proofs naturally remain open. However, any obligation affecting executable semantics must appear in the active contract, not survive only as “retain prior analysis.” B1, B2 and B4 are examples where that promotion is incomplete.

Before implementation, add a compact obligation map containing: historical item, active clause/task, changed semantics if any, and required proof. Do not copy obsolete line numbers, source templates, release targets or polling-only instructions.

## Materiality of the two missing original reports

The following reports are unavailable and custody must not be claimed:

- `2026-10-06-frontend-streaming-readiness.md`
- `2026-10-06-frontend-streaming-source-review.md`

The recovery warning explicitly confirms their absence at [recovery-2026-10-06.md:16](recovery-2026-10-06.md#L16).

My assessment is:

- Their absence is an evidentiary limitation.
- It is not, by itself, a material implementation blocker after the recovered twelve findings, expanded contract, historical task analyses, retry snapshots and current-source inspection are considered together.
- I cannot claim that every sentence or fine-grained observation from those reports was recovered.
- I found no identifiable safety domain that depends solely on an unavailable report. The important domains—transport, authentication, custody, cancellation, retry history, progress isolation, persistence, schema, atomic settlement, frontend recovery and real TLS—are all present in the supplied material.
- The implementation remains NO-GO because of B1–B5, not because I am inferring unknown contents from the missing reports.

If either report is later recovered, it should be reconciled against the obligation map, but implementation need not wait solely for archival custody once the concrete blockers above are corrected.

## Concrete bounds and gates

The proposed transport limits are reasonable experimental starting values, not yet justified production constants:

- 10-second heartbeat;
- no faster than one snapshot read per second;
- 120-second subscription;
- five-second actual ASGI send timeout;
- 64 KiB encoded frame;
- two readers per operation;
- four per principal;
- 32 per process;
- 512 KiB per submitted browser body;
- 1 MiB aggregate per principal/provider;
- 24-hour custody.

The relationships are coherent: heartbeat is well within subscription lifetime; terminal payload remains off-stream; per-process caps are honestly not cluster caps; coalescing prevents progress backlog.

The main capacity risk is 32 subscribers performing recurring authorization/job/progress reads through a shared pool that currently admits 16 running plus 16 queued synchronous calls. The plan correctly requires contention measurement and reduction, and current `run_sync_in_worker` retains capacity until the underlying thread actually finishes at [async_workers.py:194](../../../src/elspeth/web/async_workers.py#L194). Treat “32” as a maximum to test, not a target to preserve. N17 must show that subscriber reads cannot starve worker settlement, authentication or ordinary synchronous operations.

## Nonblocking concerns

- The soft cluster admission cap can overshoot by concurrent admitters. This is explicitly documented and protected by the per-session unique index; it is acceptable if measured and observable.
- SQLite loss of intermediate progress is acceptable because the contract makes progress advisory and terminal replay durable.
- A 120-second stream lifetime implies reconnect churn for long compositions. That is acceptable if reconnect backoff and permits are proven and reconnection never extends job/provider/quota budgets.
- Browser custody limits may reject large unresolved operations. Refusing new admission while retaining the unresolved body is the correct fail-closed behavior.
- The audit-only list is concrete enough to begin from, but N07’s live writer measurement remains mandatory because the supplied baseline records known unresolved writer debt.
- The planned real TLS test must prove an early bounded progress or heartbeat frame before completion. Merely receiving headers does not pass.
- Tutorial scrutiny must remain per transition, not per walk, after the terminal boundary moves.

## Review scope and limits

I reviewed only the supplied input directory and made no modifications. I did not use network, apps, providers, credentials, environment values, parent directories or the absent PR276 archive diff. No tests were run because this was a read-only plan review and no implementation exists to test.

Measured current behavior in the supplied source includes synchronous send/recompose responses, socket-owned disconnect cancellation, tail-role recompose rejection, entry-time Bearer authentication/live-role checking, the bounded shared worker helper, PostgreSQL-backed versus SQLite in-memory progress selection, and the complete-response provider path.

Everything in the async operation, stream, schema, worker and frontend contracts remains proposed design. Passing SQLite/PostgreSQL, browser, TLS, resource, whole-tree and provider-call tests remains future implementation proof.

Final verdict: **NO-GO until B1–B5 are corrected in the active contract/task pages.** Once corrected, the missing reports are not independently material blockers, and the plan is suitable to proceed into held-branch implementation followed by the required Astra and Daybreak implementation reviews, local fake-provider/TLS validation, and John’s local testing—without merge or deployment.
