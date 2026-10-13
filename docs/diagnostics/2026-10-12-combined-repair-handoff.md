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

The operator's subsequent scope decision keeps website work with 0.8.5 and
closes that release's scope except for P0/P1 changes. Website/Pages remains
outside this emergency/CSV delivery. The two authorized P1 diagnostic
extensions and their qualification continue; the publication-hold
coordination is unchanged.

Publication must use the operator-designated `johnm-dta` account. Before
publication, resolve Git's actual HTTPS credential under operation-scoped
helper settings and verify that credential against GitHub's authenticated
user endpoint. The initial check returned `johnm-dta`; a read-only remote
check found no emergency branch. No global account switch or fallback to
another account is permitted. Preserve existing commit attribution.

## Current disposition

- CSV multi-lookup: integrated in the committed history. The feature owner's
  independent correctness and security reviews accept the reviewed bytes;
  its 836 focused tests and frozen static gate passed. These precede combined
  qualification and do not substitute for it.
- D29/D30: repairs committed in `48764972c` distinguish candidate reference errors
  from saved-review integrity errors and return valid requirement IDs through
  custody-aware feedback. Focused checks passed before CSV integration.
- D21/D35: the CSV inspection repair committed in `48764972c` excludes incomplete sample
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
  confirms the fatal-assertion D20 gap, repaired in `48764972c`. Bounded
  filtered pre-admission response retention and useful cause facts are now
  implemented bounded repairs, with independent security acceptance and
  37 passing final helper/persistence controls. Final combined gates remain.
  Provider-context reconstruction remains a separate coverage limit.

Production repairs committed in `48764972c` cover `interpretation_state.py`, Composer state,
planner, source inspection, tools, skill guidance, diagnostic evidence,
session persistence/private reads, the internal audit receipt contract and
`engine/orchestrator/cleanup.py`. Focused regression tests, diagnostic
reports and synthetic exercise fixtures accompany them. The CSV plan now
states the combined delivery route rather than a standalone feature PR.

## Remaining delivery gates

1. D20 implementation and independent security review are complete for the
   filtered-evidence baseline committed in `48764972c`. The authorized
   pre-admission and cause-fact extension is implemented and independently
   reviewed. It passed 37 final helper controls and three corrected
   fatal/cancellation controls. The 269-test affected run passed before the
   final typing-only correction, so it is not frozen-final-tree evidence.
   Contract census and controlled provider-template checks pass unchanged.
   Final combined static and runtime qualification remain pending.
   Baseline final failure controls passed 33 tests;
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
7. Run every self-contained local example on the frozen final combined
   candidate through one dedicated test subagent. Inventory every entry in
   `examples/`, following its repository instructions and actual workflows.
   Exclude Azure, OpenRouter, AWS and every example needing remote APIs,
   external services/data, credentials, spending or provisioned infrastructure.
   Preserve isolated outputs and an exact passed/failed/skipped/unrun matrix
   with commands, exclusion reasons, exit codes and source fingerprint. This
   is an explicit operator acceptance requirement before landing; inventory
   preparation does not count as execution, and skipped examples are not
   passed examples. Coordinate host capacity with existing CI.
   The operator explicitly classifies genuinely broken eligible examples as
   P0/P1 blockers belonging in this emergency build. Diagnose setup failures
   separately; route product/example repairs through the single implementation
   writer and independent review, then rerun affected workflows and applicable
   gates on the updated frozen candidate. Do not weaken assertions or silently
   exclude failures.
   Treat engine regression as the leading hypothesis, then verify it against
   the intended contract and available known-good behavior. Preserve original
   examples and traces, investigate related instances, and distinguish engine
   defects from stale configurations, fixtures or setup. Never change example
   expectations merely to accommodate broken engine behavior.
8. The new `reference_join/settings_multi.yaml` example performs three
   independent lookups across products, regions and tax-rate CSVs. Its actual
   CLI regression passed: four exact enriched rows with product/region
   defaults, and one exact original row quarantined on a later tax miss.
   The intended partial-run CLI exit is 1. The whole-corpus run must include
   this variant. Separately, ordinary Composer must author and execute the
   same synthetic workflow through its normal review and admission paths.
   That live acceptance remains pending; the deterministic CLI check does
   not establish it. Existing authorized provider-call/cost/time guards apply.

Outgoing chat controls are unavailable in this repair chat. The parent bridge
is relaying coordination; read-only chat inspection remains available. This
is a communication limitation, not a request for an alternate access route.
