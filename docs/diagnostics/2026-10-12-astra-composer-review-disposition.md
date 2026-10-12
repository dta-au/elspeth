# Independent Composer review disposition

The parent coordination chat commissioned a read-only `gpt-6-astra` review
at high effort and relayed its completed report on 2026-10-12. It inspected
the evolving repair tree at committed HEAD
`852ad7d5ceffd3453d8abed71df3d9386aa88e25`; it performed no tests, edits or
provider calls. This document records the integration team's disposition,
not a new runtime acceptance result.

## Findings and scope

1. **Fatal dispatch receipt loss: independently confirmed, locally repaired.**
   A plain `AssertionError` leaves the current batch's tool receipts in the
   recorder but escapes before P4. The route's unknown-fault drain saves
   attached LLM records only. This is within D20's dispatched-tool evidence
   scope. The bounded repair transfers only the undurable batch through
   an owned carrier, persists audit evidence without publishing graph state,
   and re-raises the same original exception. Regression controls include an
   earlier durable mutation, a useful rejection followed by an assertion,
   exact-once filtered receipts, and unchanged graph state. Source review
   does not establish process-death or resource-exhaustion durability.
2. **Provider rejection before parsed candidate capture: authorized bounded
   repair implemented; final qualification pending.** The committed D20 baseline captures admitted parsed
   planner candidates. Output-limit, duplicate-ID and malformed-response
   rejection may happen earlier; existing physical-call and semantic-attempt
   records retain closed failure dispositions, not the rejected arguments.
   The authorized extension captures filtered received slots independently
   of admission, with request/physical-call/position correlation, at most
   32 slots, separate shared 1 MiB raw/canonical input budgets and a shared
   1 MiB final filtered-argument budget per response. Independent security
   review accepts the final implementation; focused controls pass.
   It does not claim complete raw-response archival or retain unsafe wire.
3. **Class-only failure detail: implemented; final qualification pending.**
   Distinct benign exception messages can become indistinguishable in durable
   Composer records. The current private evidence contract preserves authored
   arguments and returned results. The authorized extension adds bounded,
   credential-filtered built-in exception facts to private evidence, preserving
   public class-only errors. Its design limits capture to five cause/context
   nodes and 8 KiB of facts, without invoking arbitrary string conversion.
   Final helper controls pass and independent security review accepts the
   bounded implementation. No exception object, locals or
   transport credentials are retained.
4. **Provider context reconstruction: acknowledged limit, separate scope
   decision.** Request and tool-spec hashes identify a request but cannot
   reconstruct its ordered messages, transient feedback and omissions. A safe
   snapshot or complete versioned reconstruction recipe would extend the
   current evidence model. This patch does not claim reconstruction of the
   request submitted to LiteLLM or its downstream wire transformation.

The review also supports the two clarification/readiness wording findings in
the [Fable review and independent validation](2026-10-12-fable-orchestrator-review.md).
It confirms that the planner's existing marked clarification path can ask a
first-call question. These presentation recommendations are not implemented
by the diagnostic persistence repair.

## Additional cancellation completion

The independent D20 security review enumerated every `await` in `tool_batch`
and classified whether the current call was undispatched, recorded already,
protected by the existing dispatch `finally`, or still open. Eight open-call
await boundaries need matching cancellation closure: cached-discovery
progress, both proposal writers, tool-start progress, advisor hint,
interpretation-review preflight, the session-aware handler and the second
interpretation-tolerant preview preflight. The first preview preflight is
already covered. This bounded slice is implemented and its focused tests
pass; independent security review accepts the bounded repair. Already recorded
successful/error results retain their existing record without a duplicate
cancellation record.

## Verification checkpoint

Before the additional completion slice, the focused persistence/contract run
passed 61 tests. The expanded run passed 239 and failed one stale expected
error label; the corrected full route module then passed all 44 tests.
The dynamic-attribute/masquerade precheck passed 249 tests. The frozen static
gate passed Ruff, mypy and contracts, with fingerprint `a409d16b2d2ea6dc`.
Keyless lint exited 1 with 3188 findings versus 3179 in the previous repair
snapshot; the delta requires individual review and grants no operator
signature clearance. These checkpoints precede the remaining completion
slice and do not qualify the final combined candidate. The subsequent final
failure/cancellation slice passed all 33 tests, including eight direct-await
cancellations, fatal assertion persistence, overlapping caller cancellation
and required audit-failure precedence; 46 route/cancellation controls also
passed. The affected service module had 67 passes and one stale internal
carrier expectation, corrected and verified with its focused test while
production hashes stayed unchanged. Final production Ruff, mypy and contracts
passed with frozen fingerprint `91b8bdd3ba8a4a71`; keyless lint stayed at 3188
with the same reviewed delta. Full default tests,
PostgreSQL, protected landing and deployment remain separate claims.

The follow-up extension passed 37 final helper/persistence controls after a
typing-only correction, plus three corrected fatal/cancellation controls.
An eight-module affected selection passed 269 tests before that typing edit;
it is behavior evidence, not a frozen-final-tree result. The contract precheck
passes with the existing 2536-site census unchanged. The provider template
control passes on actual source and rejects an in-memory removal of required
lease context. Final combined static, PostgreSQL and broader acceptance gates
remain pending. These overlapping selections are not an aggregate count.
