# F6 independent source review

No blocking finding or required source correction identified in the specified F6 diff.

Reviewed against branch `codex/0.8.10-exceptions`, HEAD
`fe92784450918d66f3faf9560595609e28812289`. This is review of the uncommitted
working-tree diff and the new test/helper/fixture files, not approval of a landed
commit. Read AGENTS.md and CONTRIBUTING.md whole-tree guidance before review.
No source edits, tests, provider calls, commits, pin changes, or documentation
changes were made by this reviewer. This note is the only reviewer write.

## Source findings

- Both gateway wrappers classify the genuine LiteLLM BadRequestError before
  wrapping it, attach the existing SafeRequestRejection, and preserve the
  pre-existing `raise ... from exc`, `provider_detail`, status, and wrapper text
  (`provider_gateway.py:748-756`, `807-815`). No retry loop or alternate proposal
  path is added. The optional owned field also preserves historical carriers.
- The HTTP helper adds only `request_rejection.to_wire()` before its debug-off
  return (`sessions/routes/_helpers.py:758-765`). The existing debug scrubber,
  truncation, status projection, and class-name-only detail remain in place.
- The shared error builder admits a present descriptor using the existing
  isSafeRequestRejection guard before its compatibility-parser catch block;
  malformed present evidence cannot be silently discarded by that catch
  (`frontend/src/api/client.ts:314-321`). Absent evidence is omitted from the
  resulting ApiError rather than fabricated (`:570`). Both live parseResponse
  and durable apiErrorFromBody use this builder.
- The new renderer selects fixed copy from admitted code/disposition, and only
  interpolates an admitted diagnostic reference. It does not reuse the
  Advisor-specific public_detail/handoff_detail, provider prose, or guessed
  bounds. Composer keeps its existing debug appendix separate; diagnostics
  selects the same safe reason text with its own request-specific prefix.
- Traced the existing durable observer -> apiErrorFromBody -> session store
  chain and actual Composer/diagnostics backend catch owners. The new field
  survives their existing error-body transport. Required audit ordering and
  diagnostics authority/lock ownership are unchanged by the production diff.

## Test-source and receipt review

Read both complete new backend tests, both complete new frontend test files,
the shared fixture, the complete classifier and SafeRequestRejection contract,
the shared frontend guard, and the relevant route/observer/store owners.

The gateway test checks exact SDK cause identity, unchanged debug/status/text,
one dispatch, and a transient-error neighbor for both wrappers. Its byte test
uses genuine LiteLLM errors with body/code None and buffered lengths 65535,
65536, and 65537, plus malformed JSON, duplicate keys, invalid UTF-8, unknown
code, conflicting observations, and an unread stream that raises on iteration.

The Composer route test reaches the actual tool-capable dispatch, checks
required audit completion before HTTP projection, reloads the durable error
through the operation endpoint, and requires a bad-request audit row with no
assistant substitute. The diagnostics test reaches the actual text gateway
and execution route, requires the compose lock absent at dispatch and held
for real atomic audit persistence, checks exact RunDiagnosticsAuditAuthority,
and calls the real persister with a rebound state to prove refusal.

The route tests compare their actual resulting error bodies with the fixture.
The frontend tests consume that fixture through the actual snapshot decoder,
observer/shared error decoder, and stores. They cover debug off/on, later
successful diagnostics, historical descriptor absence, closed-field/evidence/
reference/nonretryable validation, all admitted reason-copy choices, and
supplied diagnostic references.

Inspected initial red logs and the dependent frontend red, plus mutation logs
for dropped gateway assignments, exclusive/enlarged size boundaries, stream
reads, omitted/debug-gated projection, skipped diagnostics audit persistence,
shared/durable decoder omissions, loose/extra-field admission, fabricated
historical evidence, omitted renderers, and provider-prose substitution.
Their failures occur at the intended assertions; no surviving mutation was
observed in the final diff.

Recorded lane receipts were inspected; they were not rerun by this reviewer:

```text
gateway-final.exit: 0
gateway-final.log: 34 passed in 5.79s
routes-final-green.exit: exit=0
routes-final-green.log: 4 passed in 17.14s
frontend-bridge-focused.log: Test Files 5 passed (5); Tests 25 passed (25)
frontend-final.exit: 0
```

No independent runtime, broad-suite, PostgreSQL, architecture-gate, deployment,
or signature-clearance claim follows from this review. Pin derivation and any
remaining permitted validation remain with their existing owners.

## Final reviewed source identity

Final `git diff --exit-code` returned 0 for provider_errors.py,
contracts/provider_rejection.py, execution/routes.py, sessions/composer_turn.py,
and frontend/api/validationReadiness.ts. Thus the classifier/guard and temporary
audit mutation targets are unchanged from HEAD.

```text
provider_gateway.py cfac2aa7bd0b192eaf6495c69e1ebdd7810234553247cdf3602548a3b9b0f829
sessions/routes/_helpers.py da4b6a51ccbf8abb4a5f3f515b0f5bc0cfbfda77d244ed6c92166171e0b8160d
frontend/src/api/client.ts 2aa4f3a747182e4f20f84d9a3c03c468216564f47e8355625c8f7c149fb37d93
frontend/src/types/index.ts e028b4345c64af14f01a6963fe14feb3ab261b18abbe256909f7c3b07da999a4
frontend/src/stores/requestRejection.ts 40164c44b3f9ca94a2c4f7a46e9d2ef4cd28d2a5dae75741869f527c7898927c
frontend/src/stores/sessionStore.ts 59fb7c33f3714e8e36943a4e3abec26d21b81c94343acfdce43665519f5f096e
frontend/src/stores/executionStore.ts f5e276051ec16664158ea9d8b0109fd5af29dbf75bbd8b0c96f0780ba67bfec9
```
