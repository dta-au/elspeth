# Combined emergency repair and CSV lookup delivery

The integration branch is `codex/emergency-diagnostics-20261012`. The
integration anchor is `852ad7d5ceffd3453d8abed71df3d9386aa88e25`. It contains CSV
lookup implementation `37ebd28e512aa6f17ddf2fa9d09d4928675225dc` and reviewed
publication-hold commits `0bf2c5d54`, `c03f4e25f`, and `5ec4fece1`, in order.
The CSV feature was fast-forwarded from
`fdaa670394fd311fc5df61befb1a7b1941c01182` with all existing uncommitted
incident repairs preserved. No duplicate cherry-pick or shared-checkout edit
was needed. This chat owns integration; implementation subagents have bounded
file ownership and do not move branches or publish.

## Current disposition

- CSV multi-lookup: integrated in the committed history. The feature owner's
  independent correctness and security reviews accept the reviewed bytes;
  its 836 focused tests and frozen static gate passed. These precede combined
  qualification and do not substitute for it.
- D29/D30: local uncommitted repairs distinguish candidate reference errors
  from saved-review integrity errors and return valid requirement IDs through
  custody-aware feedback. Focused checks passed before CSV integration.
- D21/D35: local uncommitted CSV inspection repair excludes incomplete sample
  tails while retaining real jagged-row diagnostics. Its 256 affected checks
  passed before CSV integration.
- Cleanup P2: implemented locally. Node identity now reaches per-hook and
  aggregate diagnostics, with bounded safe cause and trusted source-location
  metadata. The final focused selection passed 87 tests. Independent
  correctness and security reviews accept the combined source; final
  qualification remains.
- Combined CSV/Composer authoring seam: review reproduced rejection of nested
  uploaded-table markers by full replacement, node updates and option patches.
  Exact typed-error deferment now accepts supported markers and preserves
  unrelated errors and emitted-placeholder protections. Eighteen focused
  checks pass, including full-state round-tripping. Content-dependent model
  checks remain deferred to normal materialized validation.
- D20: credential-filtered private durable evidence is now explicitly
  authorized and implemented locally in this same delivery. A focused contract/persistence run
  passed 61 tests, including a real child cancellation through the session
  route and an aborted batch after an earlier committed mutation. Final
  expanded controls exposed additional direct async cancellation paths and a
  fatal-assertion drain gap. Their bounded repair now passes a 33-test failure
  slice. Independent security review accepts the bounded implementation;
  full combined qualification remains pending. Retaining
  credential-bearing encrypted originals is outside the authorized design.
- Local Composer exercise: completed before CSV integration in 218.889s with
  real OpenRouter calls. The authored prompt contains the full reference and
  required row fields. Two human reviews remain pending; no execution took
  place. The server stopped cleanly. This is not acceptance of the integrated
  multi-lookup feature or proof of runtime classification.
- Historical D31–D34: no additional production reproduction or repair claim.
- Claude Code Fable high review: completed read-only with process exit 0.
  The [report and independent validation](2026-10-12-fable-orchestrator-review.md)
  confirm clarification/readiness wording gaps and delayed blob-backed lookup
  feedback; a controlled probe also reproduced a misleading UTF-8 warning at
  the sample boundary. The broad claim that the planner cannot ask a first-turn
  product question is contradicted by its existing marked-clarification path.
  These review findings are not newly implemented repairs or D20 acceptance.
- Independent Astra high review: the [disposition](2026-10-12-astra-composer-review-disposition.md)
  confirms the fatal-assertion D20 gap. Pre-admission response retention,
  exception-detail evidence and provider-context reconstruction are explicit
  coverage limits requiring separate scope decisions.

Uncommitted production paths cover `interpretation_state.py`, Composer state,
planner, source inspection, tools, skill guidance, diagnostic evidence,
session persistence/private reads, the internal audit receipt contract and
`engine/orchestrator/cleanup.py`. Focused regression tests, diagnostic
reports and synthetic exercise fixtures accompany them. The CSV plan now
states the combined delivery route rather than a standalone feature PR.

## Remaining delivery gates

1. D20 implementation and independent security review are complete for the
   defined filtered-evidence contract. Final failure controls passed 33 tests;
   route/cancellation controls passed 46. The full affected service module had
   67 passes and one stale internal exception expectation. Its isolated repair
   passed, retaining original-cause, current-receipt and no-publication checks;
   all 13 production hashes stayed unchanged. No full-module rerun is claimed.
2. The pre-D20 frozen static gate passed Ruff, mypy and contracts, with
   unchanged fingerprint `5549f212ba1a988f`. Keyless lint exited 1: 3179
   findings versus the reviewed CSV baseline's 3174. The controlled comparison
   found five additions and no removals, explained by cleanup diagnostic
   classification/omission and an existing catch's stale binding. This does
   not establish operator signature clearance.
3. The pre-D20 frozen affected selection exited 1: 3393 passed, one failed,
   three warnings, 413.68 seconds. The sole failure was a historical 73-byte
   growth control against a prompt with 163 bytes of headroom. Its reviewed
   test-only repair retains the ceiling and checks exact-ceiling and plus-one
   boundaries. Both focused cases pass (exit 0, 3.61 seconds). These results
   do not qualify the subsequent D20 implementation.
4. This branch carries the reviewed publication hold through `5ec4fece1`;
   its five files match that commit. PR #290 stays unmerged with auto-merge
   off so it cannot race this candidate. Notify its integrator when this
   delivery lands. Shared broad-test capacity remains with active CI; do not
   start a duplicate broad suite.
5. The final production static gate passed Ruff, mypy and contracts with
   frozen fingerprint `91b8bdd3ba8a4a71`; attribute/masquerade checks passed
   249 tests. The subsequent isolated test repair passed Ruff/format and its
   focused test without changing production. Final keyless lint remains at
   3188 findings: 10 added and one removed against the pre-D20 3179 snapshot,
   unchanged from the independently reviewed interim delta. All 118 warnings
   are unchanged. This is reviewed policy delta, not signature clearance.
   Full default-selection and PostgreSQL qualification remain required before
   landing. Use shared CI capacity rather than launching a competing broad
   local suite; reconcile target changes before claiming final qualification.
6. Commit only owned reviewed paths, publish the combined PR, and require
   current-head checks before normal protected landing. No deployment or
   protection bypass is authorized by this handoff.

Outgoing chat controls are unavailable in this repair chat. The parent bridge
is relaying coordination; read-only chat inspection remains available. This
is a communication limitation, not a request for an alternate access route.
