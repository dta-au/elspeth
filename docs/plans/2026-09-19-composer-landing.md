# Composer decision panel landing plan — 2026-09-19

## Starting point

- `release/0.8.1` was `8f4384936aac64e34e8c617a635f092c261ba559` at
  this audit. The product candidate is `integration/composer-unlanded-20260919`
  at `6c93a87deb7034209ab355b977d14fa09a3f0aff` before this plan update:
  the decision panel commit and six adapted decoder checks. Recheck both tips
  before integration; neither name is a frozen SHA.
- The [donor reconciliation](../agents/composer-branch-reconciliation-2026-09-19.md)
  accounts for the older Composer trees. Fourteen donor worktrees are removed;
  their branch refs remain. No additional donor patch is queued for merge.
- At the initial handoff, Phase 1 bug `elspeth-cb0d4b8dba` was `fixing`
  with an expired claim. The independent landing review reclaimed it as
  `codex-composer-landing`. Phase 2 task `elspeth-b0ef01ea25` remains separate.
- On the frozen product candidate, frontend Vitest finished with 4,767 passes;
  typecheck, ESLint, Stylelint and build exited 0. Ruff, mypy and contracts
  exited 0. The key-free trust-tier lint corpus matched release exactly at
  2,130 findings. Default pytest finished with 116 failures. Three distinct
  failures reproduced serially on the exact release base: session mutation
  authority, guided LLM-call audit, and freeform graph parity. An additional
  full release baseline was interrupted after slow progress at 89%, so the
  other candidate failures are not yet attributed.

## Independent review checkpoint

The resumed review rechecked release at `8f4384936` and integration at
`70a384f3d`; the remote release also matched that base. Evidence for this
review is under `.claude/lanes/composer-landing-review-20260919/`. Terminal
gate results and the exact landed/publication SHA belong in the Phase 1
ticket; this checkpoint does not certify a gate still in progress.

- The current workspace already makes `Open checks` reveal Pipeline and focus
  Checks at narrow widths. A regression exercises the real panel and workspace
  at 375px; removing `showPipeline` makes it fail. The older donor warning
  described behavior that is no longer present.
- The panel was absent from completed guided sessions. The repair makes
  readiness visible above their advisory chat input and directs pipeline
  suggestions to the existing freeform editor. Active guided decisions retain
  their existing controls. No tutorial-specific authoring path is added.
- Interpretation arrivals were announced by both the existing acknowledgement
  region and the panel. The repair keeps their existing announcer and derives
  pointers from the same selection as the actual cards.
- Browser acceptance exposed a collapsed decision dock on narrow screens.
  The repair allocates visible space to both the transcript and decision dock,
  wraps long terms, and places completed-guided cards and summary in the
  existing scroll area so the chat input remains reachable. Existing approval
  controls retain their behavior and accessible names.
- Serial reproductions on base and candidate show that provider fakes replaced
  the private wrapper that now owns physical-call admission. Moving the fake
  to `litellm.acompletion` exercises real admission and settlement. Lifecycle
  fakes also need the owned governance field, Azure assertions need the existing
  zero SDK retry policy, and Sessions writer location pins need the measured
  comment-only line shift. These repairs are tracked as `elspeth-8b85e21af9`.
- Restoring the real provider boundary also exposed a diagnostics defect:
  omitting the optional recorder left an admitted call without terminal audit
  evidence. The repair creates an internal buffer when no recorder is supplied;
  provider errors and cancellation must retain their original behavior. This
  production audit repair requires the serial PostgreSQL gate as well as the
  default suite and a key-free trust-tier comparison.

## 1. Make the release test baseline actionable

1. Freeze the current release SHA and run the canonical default gate in an
   isolated, clean release worktree. Capture terminal exit, JUnit and frozen
   tree summary. Bound any slow tranche and record an incomplete run as such.
2. Compare its failure IDs with the candidate JUnit, then rerun differences
   serially on both trees. Group repeatable failures by the actual broken
   contract. The current candidate log suggests at least LLM-call audit
   evidence, test contexts missing `llm_call_governance`, session writer
   identity expectations, and Azure retry construction; these are leads, not
   an attribution of all 116 failures.
3. Repair confirmed release defects in owned branches and link them to existing
   legacy issue tracker issues or file focused issues where none exist. Keep those repairs
   separate from the frontend panel diff. Run focused regressions first, then
   the default suite on the integrated tip. Any schema, session persistence,
   audit storage or lock repair also needs the serial PostgreSQL testcontainer
   selection.

**Exit:** the exact release tip intended for the panel merge has a completed
default pytest run with exit 0. Candidate-only failures have been fixed or
explained with reproductions; a partial baseline is not a pass.

## 2. Finish and land Phase 1

1. Review the panel on the rebased candidate in freeform and guided sessions,
   including a real blocked state, Apply, pending interpretation and proposal
   pointers, keyboard focus, live announcements and a narrow viewport. Verify
   `Open checks` navigates to the visible Pipeline Checks view and that every
   panel action remains reachable in the bounded dock. Keep the reload loss of
   Apply suggestions and missing advisor suggestion as explicit Phase 2 wire
   work.
2. Preserve the tested removal of `Review again`. The ticket's later comment
   explains that a no-mutation compose turn does not persist a new completion
   gate and can deepen the block. Every pipeline-changing Apply stays on the
   normal provider-backed Composer path; no tutorial or server-authored path.
3. Reconcile the moving release tip. Rebase or recreate the integration branch
   with shared `rerere` disabled, and rerun affected frontend checks, full
   Vitest, typecheck, lint and build. Run the frozen full backend gate on that
   final tip. Compare key-free trust-tier findings with the base without trying
   to sign or clear the package-level gate during feature work.
4. After the code and test gates pass, locally fast-forward the exact validated
   commit into `release/0.8.1` with shared `rerere` disabled, then verify the
   final tip and ancestry. Leave the known key-free signature failure visible
   while package changes continue. At package completion the operator signs;
   verify the protected CI and remote publication against the final SHA before
   moving `elspeth-cb0d4b8dba` through verification and closing it with the
   landed SHA and acceptance evidence.

**Exit:** release contains the panel and decoder checks, the responsive action
works, the required gates have terminal evidence, and the Phase 1 ticket is
closed against the published commit.

## 3. Complete Phase 2 after Phase 1 lands

1. Write the spec required by `elspeth-b0ef01ea25`. Resolve the ticket's
   explicit product choice about retaining anchored interpretation cards as
   history versus replacing them with panel interactions. Preserve the
   existing approval wording and accessible names.
2. In small wire commits, backfill `validation_suggestions` after reload and
   carry the backend-computed advisor suggestion on the readiness blocker.
   Prove source ownership and safe redaction; avoid a frontend guess. Run
   backend route/contract tests and PostgreSQL tests if persistence or schema
   changes.
3. Move proposal accept/reject and interpretation approval actions into the
   panel. Keep the existing APIs, reject confirmation, proposal eligibility
   predicate, one live announcement and guided/freeform parity. Check the
   InlineSourceFallbackPrompt and guided decision surfaces for duplicate
   decisions. Run focused tests, full Vitest, axe and browser flows, then the
   frozen backend/CI gates on the final tip.
4. Merge and close Phase 2 only after live browser acceptance and final-tip
   verification. Reconcile the separate live-review ticket
   `elspeth-3983cd84f1` against release independently; its tracker state is
   not evidence that more code from these donors is missing.

After both phases land, verify donor branch ancestry or patch equivalence and
retire those refs. Keep the local unique-file rescue archive until that final
check is complete; it is not a merge input.
