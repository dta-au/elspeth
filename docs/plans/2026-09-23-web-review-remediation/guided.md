# Guided findings after complete removal

Updated 2026-09-30 for local `release/0.8.1` recovery from `a2f0281ff`.
The original G1–G6 findings remain in the
[pinned web review](../../reviews/2026-09-23-release-0.8.1-web-review/README.md).
This sheet records their useful acceptance requirements under the later
architecture; it does not claim that the checks below passed.

## The later owner decision governs

The original 2026-09-23 investigation assumed Guided machinery would remain
behind the tutorial. The [complete removal plan](../2026-09-28-guided-mode-removal.md)
supersedes that assumption. Commit `7001600fe` removes Guided Composer while
retaining onboarding through the ordinary freeform Composer.

At the recovery base, `src/elspeth/web/composer/guided`,
`src/elspeth/web/frontend/src/components/chat/guided`, and
`src/elspeth/web/sessions/_guided_step_chat.py` are absent.
`src/elspeth/web/frontend/src/components/tutorial/TutorialFreeformShell.tsx`
is present. Source absence establishes that the old repair targets are retired;
it does not establish release gates, served frontend bytes, or live tutorial
acceptance. Do not recreate the removed components to satisfy old test names.

## Disposition and surviving checks

| Finding | Original defect | Current work to verify |
|---|---|---|
| G1 | Proposal action cleanup was fenced by a Guided publication generation, leaving its busy flag set after a transition | Inspect ordinary `sessionStore.ts` proposal-action ownership. Exercise success and conflict/error, switch A→B→A, and same-proposal newer-action races. An old request must release only its own busy flag and must not publish into the newer session/action. Guided-specific transition tests are retired. |
| G2 | Blank observed CSV headers could create an impossible Guided inspection turn | The inspection turn is removed. Inspect surviving upload/schema admission and prove unusable headers receive actionable refusal while a corrected upload can proceed. Preserve exact observed headers; do not silently rename facts. The original duplicate-header claim was refuted and must not be promoted to a new confirmed defect without reproduction. |
| G3 | Dead tutorial prop and obsolete editor/focus comments in Guided turns | Verify the retired Guided components and their imports/callers are absent. Keep live tutorial controls and their ordinary Composer ownership. No new unit test is needed for old comments. |
| G4 | A prefilled JSON object/array was rendered as its default string coercion in `SchemaFormTurn` | The Guided schema form is removed. If a maintained form accepts JSON options, verify its own rendered value and submission behavior using its current contract; source absence alone does not demonstrate a new form defect. |
| G5 | Unreachable JSON string branches and an inaccurate error comment in the same form | Removed with the old form. Inspect maintained JSON parsing/error behavior only where a current caller exists; do not port dead branches or comment tests. |
| G6 | Guided step-chat diagnostics docstring misstated the canonical service's exception policy | `_guided_step_chat.py` is removed. Keep the current shared diagnostics owner and its actual exception policy; this finding does not authorize broadening catches or suppressing audit failures. |

Coordinate G1 with the owner of `sessionStore.ts`; one writer owns that file.
Coordinate any surviving G2 failure with source validation ownership. Original
Guided detail and line references are historical evidence recoverable from
the review and donor worktree, not current implementation instructions.

## Acceptance evidence

Use existing ordinary proposal/store tests and
`TutorialFreeformShell.test.tsx` / `TutorialFreeformShell.integration.test.tsx`
for the surviving behavior. Confirm exact available scripts and test selections
from the candidate's `package.json` and current source before running them.
Use the existing `tests/e2e/tutorial.spec.ts`, staging tutorial driver/harness,
and `evals/composer-harness/hardmode/harness.sh` for their documented scopes.
Do not introduce another tutorial runner.

Required evidence before closing an applicable surviving requirement:

- The actual production caller and the current failure or absence proof.
- A focused regression with its exited result and appropriate stale/error
  negative controls; direct source review suffices for deleted comments.
- Tutorial authoring transitions continue to use the same provider-backed
  backend as ordinary sessions. Count provider calls per authoring transition
  when reviewing latency/cost changes; a total across the walkthrough is
  insufficient.
- Browser acceptance identifies the candidate and served frontend assets.
  Mocked/seeded UI checks and real-provider/live-run acceptance remain separate.

Broaden to PostgreSQL checks if a surviving repair changes persistence or locks.
The integration coordinator owns any broad suite and release inclusion proof.
Record `removed`, `needs surviving-path verification`, or a reproduced current
defect with exact evidence; do not close all six from a removed toggle alone.

## Tracker context

GitHub Issues is the shared system of record. Historical Guided retirement was
mapped to issue #191; its current status and assignee were not refreshed by
this document recovery. Legacy `elspeth-*` records and retired local tracker/
code-index outputs are archived evidence. Use current GitHub mappings and
source reads for coordination. Do not recreate retired tools, publish duplicate
issues, reopen migrated records, or claim a ticket's state proves code delivery.
