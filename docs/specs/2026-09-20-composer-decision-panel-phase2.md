# Composer decision panel Phase 2

Target: `release/0.8.1`; issue `elspeth-b0ef01ea25`. Reconciled release base:
`627b72cf1a0bcadfa260bfbbd9a111df79eb7b98`. The initial remote snapshot was
`ebc4720e0d4f18810353c646a093269a4ba7bc81`; the reconciled base preserves
the local release commits ahead of that published snapshot.

## Decisions and interaction ownership

Operator decision, 2026-09-20: keep anchored interpretation cards as read-only
history. The decision panel owns pending actions. Preserve event identities,
node distinctions, timestamps, transcript anchors and the labelled
Interpretation approvals tail required by `elspeth-3574f87208` and
`elspeth-52be5924d7`. Do not deduplicate resolved history by user term.

Render pending proposals as native panel list rows. Move `actionableProposals`
to a pure shared module, retaining its pending-and-not-stale predicate. Remove
PendingProposalsBanner after migrating its confirmation, busy state, effects
description and accessible names. Transcript proposal details become read-only
for pending actions; accepting or rejecting is offered once, in the panel.
Reject opens the existing confirmation dialog; cancelling preserves the row.

Render interpretation interactions in the panel through the existing
AcknowledgementCard/resolver behavior. Reuse the shared ordered pending
selection and focus restoration, removing external actionable stack mounts.
Retain prompt-view gating, current prompt display, amendment validation,
error presentation, opt-out confirmation and existing API calls. Preserve
the `ack-card-<event id>` action anchor in the panel so guided blocker links
reach its actionable destination. Read-only history must use distinct DOM ids.
The labelled approvals history remains separate from pending action controls.

The inline source fallback is a pending decision and belongs in the panel.
Add a pure row carrying the candidate text only when the existing eligibility
predicate permits it. Preserve Create source, Dismiss and session dismissal;
Create source uses the existing provider-backed conversational handler.
No server-generated pipeline or tutorial-specific behavior is introduced.

GuidedDecisionSheet is settled history: preserve replay without pending action
controls. Guided stage choices remain stage choices unless they duplicate a
proposal or interpretation already owned by the panel. Active guided,
completed guided and freeform share interpretation and proposal ownership.
Active guided currently has no panel mount; add the shared bounded dock there.
Preserve completed-guided restrictions on pipeline-changing suggestions.

## Accessibility and layout

Keep the pure row projection DOM-free. Render one decision-panel landmark,
with native proposal rows and an Interpretation approvals group. Keep exact
Accept proposal: <summary> and Reject proposal: <summary> names. Preserve
interpretation names and confirmation focus behavior. Restore focus after
removal to the next available action or a stable panel/input fallback; arrivals
never steal focus.

One always-mounted panel status region announces decision arrivals. Retire
proposal and acknowledgement arrival announcers from ChatPanel when their
ownership moves. Track identities as well as counts so replacement at equal
count is observable, while rerenders of the same decisions do not reannounce.
Keep live regions outside transcript logs. Preserve the bounded dock and
keyboard-scrollable transcript, reachable input at 320×568, and Open checks
navigation to the visible, focused Checks tab.

## Separate wire changes

1. Reload suggestions: derive validation_suggestions from the current state's
   authoritative validation result on GET /state. Reuse existing producer and
   redaction paths. No provider call on reload and no stale previous-version
   suggestion cache. Test actual state reload and invalidation paths.
2. Advisor suggestion: preserve the backend-computed suggestion through its
   owned completion/readiness representation, response model and strict
   frontend decoder. Render it on the matching blocker without inventing
   frontend advice. Omit or clear it when its owning blocker/state is absent.
   Test redaction and malformed wire values as well as successful display.

The advisor suggestion is required and nullable in the current wire and durable
envelope. Session schema epoch 62 rejects earlier stores at startup, before an
old blocked envelope can fail during reload. This semantic JSON grammar cut
follows the epoch 24/25 precedent and the repository's no-compatibility policy.
Deployment requires operator-authorized session store recreation; no migration
or shared database reset is performed by this task. Landscape remains epoch 42.

Implementation inspection must establish the exact durable owner before
editing either wire. Any required persistence change uses the existing typed
writer authority and receives serial PostgreSQL validation.

## Verification and delivery

Reproduce each defect with meaningful failing regressions before repairs.
Review all changed product files. Run full frontend tests, typecheck, ESLint,
Stylelint, build, axe and browser acceptance for reload/Apply, prompt approval,
amendment, proposal accept/reject/cancel, guided/freeform parity, focus,
announcements and narrow scrolling. Record which flows use real backend
requests, synthetic states, interception or successful live providers.

Run terminal frozen-tree backend gates on the final candidate. Attribute any
failure through a measured current release base. Run serial PostgreSQL tests
for persistence/audit/schema/lock changes and reconcile reviewed writer pins.
Compare key-free trust-tier finding sets against the current base; do not
hold a signing key or clear the package signing gate. Leave existing accepted
xfails unchanged.

Reconcile release movement with rerere disabled, run branch-safety checks,
land the exact validated tip and publish via normal non-forced push to the
authorized release branch. Verify local/remote SHA and CI. Record evidence and
external gates in the ticket, then clean up only owned disposable work.
