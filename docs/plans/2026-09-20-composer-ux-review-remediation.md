# Composer UX review remediation (2026-09-20)

Remediation plan for the findings of a single-pass UX review of the web
composer frontend changes made on 2026-09-20 (24 commits touching
`src/elspeth/web/frontend`).

## Scope and limits of the review

- Read from code and CSS only. Nothing was viewed in a browser, so layout
  findings (L8, P9) are inferred from the stylesheets and should be confirmed
  live before they are fixed.
- Two trees were reviewed and are kept apart below:
  - **Composer** — `release/0.8.1` at `f6df9d0d5` (20 commits).
  - **People & access** — reviewed on `design/people-access-panel` at
    `c7c3bec8b`. That branch has since merged into local `release/0.8.1`
    (`271b238a0`, 2026-09-21) with the implementation-review repairs, and the
    branch is deleted. P1–P9 were re-measured at `271b238a0`; all nine are
    still open, and their fixes now belong on `release/0.8.1` like everything
    else.
- All paths are relative to `src/elspeth/web/frontend/src/` unless they start
  with `src/elspeth/`.
- Line numbers are as read on the commits above; re-locate by symbol before
  editing.

## Decisions

Ruled by John, 2026-09-20:

| # | Ruling | Affects |
|---|--------|---------|
| D1 | Yes — guided mode is reachable only through Preferences, for new sessions. Freeform is the first-class surface for now; revisit after the soft launch. L2 is therefore cleanup, not a restore. | L2 |
| D2 | The approvals record lives in Graph → Approvals. Fix the tutorial copy to point there; do not mount it in Checks. | L1 |
| D3 | Yes — deleting a local account requires a reason, recorded in the audit trail. Needs a backend parameter on the delete route as well as the form field. | P1 |
| D4 | Option C — blocker rows get "Ask the composer about this", which drafts a request into the chat input for the user to send. See L5 for the options considered and the live-session evidence. | L5 |

## Composer (release/0.8.1)

### High

**L1. The tutorial sends users to the wrong tab for their approval record.**
`components/tutorial/copy.ts:83,96` (reworded in `f74762d36`) says the record
can be revisited "in your pipeline's Checks tab". Checks shows only a count row
(`components/audit/AuditReadinessPanel.tsx:142`, `formatLlmInterpretationsRow`).
The name, value and date record renders in `components/inspector/GraphApprovals.tsx`.
Measured: the non-test components that read `accepted_value` are
`GraphApprovals.tsx`, `composer/NarrativeResults.tsx` and
`chat/promptTemplateDisplay.ts`; none is under Checks.

- Fix (D2 ruled): change both copy strings to name the Graph tab's Approvals
  table. Checks keeps its count row.
- Check for the same "Checks tab" claim outside the tutorial (help text,
  `docs/`, teaching skills) and correct it there too.
- Verify: a test that pins the tab named in the copy to a surface that renders
  `accepted_value`.
- Existing pin (go/no-go review, 2026-09-21):
  `components/tutorial/TutorialTurn7Graduation.test.tsx:364-387` asserts
  `/your pipeline's Checks tab/` and `/each pipeline's Checks tab/`, written
  for elspeth-4f69b267dd to keep the copy on a tab that exists. D2 supersedes
  the wording, not the intent: rewrite the test to pin the Graph → Approvals
  wording; do not delete it. The other "Checks tab" hits in the tree
  (workspace chrome, action bar, E2E geometry and accessibility specs, the
  decision-panel phase-2 spec) are the tab's own name and stay as they are.

**L2. Guided mode has no in-session entry point.**
The only mount of `ModeSwitchButton` is `target="freeform"`
(`components/chat/ChatPanel.tsx:3186`); a grep for `target="guided"` finds the
header comment only. The comment at `components/chat/guided/ModeSwitchButton.tsx:2`
still says "One component for BOTH directions".

- Fix (D1 ruled: intended, revisit after soft launch): correct the
  `ModeSwitchButton.tsx` header comment; amend
  `docs/plans/2026-09-20-composer-freeform-default.md` step 2 so the plan
  matches what shipped; and make the Preferences legend tell a user in a
  Freeform session how to get a guided one ("Default mode for new sessions" is
  there; "start a new session to use it" is not).
- The `target="guided"` arm of `ModeSwitchButton` is now unmounted. It is
  unfinished intent, not debt: D1 says the in-session switch is to be
  revisited. Leave it, and say so in the header comment, rather than delete it.

**L3. Apply is the least prominent button in the decision panel.**
`components/chat/DecisionPanel.tsx:302` renders Apply as `variant="bare"`;
a proposal's Accept in the same panel is `variant="primary"` (line 269). The
panel exists because of the 2026-09-13 ruling that the fix affordance was
buried (elspeth-cb0d4b8dba).

- Fix: give Apply a filled, non-bare variant. Where several suggestions are
  listed, a secondary filled variant avoids a column of primaries; one
  suggestion alone can be primary.
- Verify: existing `DecisionPanel` tests still find `Apply suggestion: …` by
  accessible name; add a class or variant assertion if the project pins
  variants elsewhere.

**L4. The Approvals table shows the current node binding beside the old
approved value, marked only by a hover title.**
`components/inspector/GraphApprovals.tsx:102-106`: the binding span carries
`title="Current node configuration"` and nothing visible. If the model or
profile changed after approval, the row pairs a past approval with a present
binding. Hover-only disclosure was ruled against on 2026-09-13.

- Fix: make the distinction visible text, for example a "Now:" prefix on the
  binding, or a separate "Current configuration" column. Remove the `title`.
- Verify: `GraphApprovals.test.tsx` asserts the visible label.

### Medium

**L5. Blocker rows are text with no action.**
`components/chat/DecisionPanel.tsx:204`. With blockers and no suggestions, the
only button is "Open checks" — the sub-tab the panel was built to replace.

What a blocker row is today: the backend's `ValidationReadinessBlocker`
(`src/elspeth/web/execution/schemas.py:250`) carries `code`, `component_id`,
`component_type`, `detail` and `suggestion`. The panel prints `detail` (plus
`suggestion` when present) and discards `component_id`. Of the construction
sites read, only the advisor sign-off blocker
(`src/elspeth/web/execution/completion_gates.py:139`) ever sets `suggestion`;
the rest pass `None`, and their `detail` is written for a developer, for
example "transform X uses an unsafe sequential multi-query LLM retry budget"
or "Bounded source proof blocked execution: <code>."

Blockers fall into two groups, separable without reading codes:

- **Step-scoped** (`component_id` set): retry-budget policy, managed-identity
  policy, advisor sign-off on a node. The user or the composer can change the
  step.
- **Session-scoped** (`component_id` null): no composition state, source
  inspection unavailable, proof diagnostics. Nothing the composer edits clears
  these; a fix button would promise what it cannot do — the same hazard the
  panel's header comment gives for having no "review again" button.

Options for D4:

- **A. Text only** (today). Cheapest; leaves the panel's purpose half met.
- **B. "Show step"** on step-scoped rows: select the node in the graph and
  name it with `phraseFor`, as suggestion rows already do. No provider call,
  no promise of a fix. `chat.css:2321` already styles a
  `.decision-panel-show-btn` that nothing renders.
- **C. "Ask the composer about this"** on step-scoped rows: place a drafted
  request in the chat input for the user to edit and send. The provider does
  the work (invariant 1), the user sees exactly what is asked, and nothing is
  sent on their behalf. Not offered on session-scoped rows.
- **D. One-click "Fix"** that sends the turn itself, like Apply. Not
  recommended: unlike a validator suggestion, a blocker has no server-vetted
  remedy text, so the button would promise an outcome the planner may not
  deliver.

**D4 ruled (John, 2026-09-20): option C.** John's observation, checked against
the live test-account sessions the same day: the planner already explains the
blocker in its reply, and explains it better than the panel does.

Evidence, session `46224626` (reference join + `chaosllm` profile), final reply
at 07:48:45Z. The model wrote a section headed "The blocker: the `chaosllm`
profile can't be used from here", said in plain words that the operator
profile resolves to a private endpoint and web-authored pipelines may not send
the server-held key there, said "there is nothing I can edit to make it pass",
and offered two ways forward (operator fixes the profile; or swap to `sonnet`,
which changes the behaviour asked for). The server notice appended below it
read "Web-authored OpenRouter LLM nodes may not override base_url…". Session
`7016943a` shows the same for the review-pending case: the reply names both
cards and says they must be reviewed before the pipeline proceeds.

What this changes:

- The chat reply is the good explanation; the panel's `detail` string is the
  developer-worded duplicate of it. A plain-words rewrite of every backend
  `detail` is therefore lower value than first thought. Keep `detail` short and
  let the row lead to the reply.
- The "step-scoped means the composer can fix it" split above is wrong as a
  fixability test: this blocker was step-scoped and not fixable by any edit.
  That is a further reason to build C ("ask") and not D ("fix"). Offer C on
  every blocker row, session-scoped included — asking is always honest.
- Build: "Ask the composer about this" drafts a request into the chat input
  (blocker `detail`, plus the step's plain name from `phraseFor` when
  `component_id` is set) for the user to edit and send. Nothing is sent on
  their behalf.
- Mechanism (go/no-go review, 2026-09-21; corrected during implementation):
  `ChatPanel` owns the freeform draft (`inputText` / `setInputText`), so the
  handler sets it directly and focuses the input. `PREFILL_CHAT_INPUT_EVENT`
  is for surfaces that do not own the draft and was not needed. The draft
  REPLACES the input, so the row's button is disabled, with a visible reason,
  while the input holds text the user typed — a button that silently
  discards typed text is the hazard P5 exists to remove. The blocker `detail`
  can interpolate user-authored names; put it in the draft as a quotation, not
  spliced into the request sentence.
- Optional companion, no provider call: when the blocked composition state has
  an assistant reply (`composition_state_id` on the message), a "See the
  composer's explanation" control that scrolls the transcript to it.
- Verify: a test that the draft lands in the input unsent, and that the row's
  accessible name includes the blocker text.

**L6. The panel count mixes required blockers with optional suggestions.**
`components/chat/decisionPanelRows.ts:183` sets `count = rows.length`, and the
heading reads `Awaiting your decision (N)`. Nothing marks which rows block.

- Observed live (session `46224626`, 08:05:30Z): while Run was blocked by the
  `chaosllm` endpoint policy — which no pipeline edit could clear — the user
  pressed Apply on the retention-routing suggestion. The composer made the
  edit; the block was untouched. The Apply button was the only button in a box
  headed "Run pipeline is blocked", so it read as the fix.
- Fix: label suggestion rows as optional in visible text (and in the Apply
  button's accessible name), or group rows under "Must resolve" and
  "Suggested". Keep the live-region count consistent with whatever the heading
  counts.

**L7. "Output" has two meanings inside one table.**
`components/inspector/GraphOutputs.tsx:95-114`. The section title and the
"Success output / Failure output" columns mean *route*; the row kind `Output:`
means *sink*, and its success cell reads "Row sunk". The "Node" column header
also heads `Source:` and `Output:` rows.

- Fix: rename the section and columns to routing vocabulary (for example
  "Routing", "On success", "On failure"), rename the first column "Step" or
  "Component", and replace "Row sunk" with plain words ("Row written").
- Verify: `GraphOutputs.test.tsx` and `GraphView.test.tsx` pin the headings;
  update them with the rename.

**L8. Two stacked detail tables at the foot of the graph canvas.**
`components/inspector/inspector.css:372` caps each scroll box at `12rem`;
`GraphOutputs` is `open` by default and `GraphApprovals` is closed. Both open
is about 24rem of canvas. An approved multi-line prompt
(`.graph-approvals-value` is `pre-wrap`) can be taller than its scroll window.
`.graph-detail-table-scroll` has `overflow: auto` and no `tabIndex`; the
project already requires keyboard-focusable scrollers (`components/chat/chat.css:1113`,
WCAG 2.1.1).

- Confirm live first (short laptop viewport, one long prompt approval).
- Fix: add `tabIndex={0}` plus an accessible name to both scroll wrappers;
  clamp long approved values with an expand control; choose one default-open
  policy for both tables (both closed is the smaller canvas cost).

**L9. A malformed approval event blanks the whole Graph tab.**
`components/inspector/GraphApprovals.tsx:77` throws during render and
`GraphView` mounts no error boundary. This is consistent with fail-closed
doctrine; recorded as a consequence. If a fix is wanted, the narrowest one is
an error boundary around the two detail tables so the graph survives and the
table shows the failure.

**L10. Every Apply button reads "Applying..." during a compose.**
`components/chat/DecisionPanel.tsx:313` keys the label on the panel-wide
`isComposing`.

- Fix: track the suggestion id being applied; show "Applying..." on that row
  only and leave the others disabled with their "Apply" label.

### Low

**L11. The stale-notice filter matches an exact string.**
`components/chat/MessageBubble.tsx:14` duplicates the wording at
`src/elspeth/web/composer/no_tool_policy.py:353`. A backend wording change
brings the stale "review the pending assumptions" notice back.

- Fix: pin the pair with a test that reads both, or have the segment carry a
  stable notice code the frontend filters on. This is a soft seam of the kind
  `explore-and-pin` covers.

**L12. The Secrets panel's mode statement is styled as a footnote.**
`components/settings/SecretsPanel.tsx:249` uses `secrets-footnote` for the
sentence that explains why the form is absent, and says "server-only mode".

- Fix: render it as a notice at the top of the body and drop the mode name:
  "Secrets are configured by an administrator on this deployment. Personal
  keys cannot be added here."

## People & access (merged to release/0.8.1 at `271b238a0`)

Paths below are under `components/admin/`. Line numbers are as measured at
`271b238a0`.

What the merge changed for this plan. The implementation review
(`docs/plans/2026-09-20-people-access-implementation-review.md`, nine findings,
repaired in `f00769a6f` and `fa7aeb67a`) covered credential retention,
pagination, selection integrity, search and recovery. It does not overlap
P1–P9: none of them was fixed by it. Three repairs change how a finding here
should be fixed, noted under P1, P4 and P5.

Keep these design decisions through any rework: one focus trap with inline
confirmations; the one-time password region is not a live region; the
"this is not the same as disabling" consequence list; the contextual "Back to
pending access" label; several one-time passwords held on screen at once.

### Medium

**P1. Deleting a local account takes less effort than disabling one.**
`AccessSection.tsx` requires a typed reason to disable. `SignInSection.tsx:119`
deletes in two clicks and `deleteAdminUser(username)` (`api/client.ts:584`)
sends no body.

- Fix (D3 ruled): add a required "Reason" field to the delete confirmation,
  matching Disable access; carry it through `deleteAdminUser` and the delete
  route into the audit record. The CLI `users remove` path should take the same
  reason so the two surfaces record the same thing.
- Since the merge there are two delete call sites. The review repair added a
  "Finish removing <name>" retry (`SignInSection.tsx:65`) for a deletion whose
  credential write landed and whose retirement did not. The retry must send the
  reason the administrator already typed (hold it beside `deleting`), not ask
  again and not send none. Decide what the audit record says when the reason
  arrives on the second attempt only.
- This touches the auth backend (`admin_routes.py:196`, and `retire_identity`,
  which the repairs just changed to take `credential_exists`): run the affected
  Python tests, and the testcontainer selection if the change reaches
  persistence.
- Measured path (go/no-go review, 2026-09-21). The recorded reason is a
  literal today: `local_identity_retirer`
  (`src/elspeth/web/coordination/identity_authority.py:3229`) passes
  `reason="local credential deleted"`, and its docstring says provider, subject
  and reason are "decided in exactly one place (elspeth-9c171c00fa)". Keep that
  rule: the administrator's text is an INPUT to the retirer, which still
  composes what is recorded; no surface writes the recorded reason itself. The
  change reaches the `RetireIdentity` callable signature,
  `LocalAuthProvider.delete_user` / `_retire_identity`
  (`src/elspeth/web/auth/local.py:630,682`), the wiring at
  `src/elspeth/web/app.py:1146`, the route, and `composer_users_remove`
  (`src/elspeth/cli.py:2150`). Both audit sinks (`app.py:1056`,
  `cli.py:2017`) already write `outcome.reason` to the same Landscape row and
  to `identities.disable_reason`, so no new column is expected — confirm the
  column's length or CHECK constraint before starting; that measurement, not a
  guess, decides whether the testcontainer selection is owed.
- Ten test sites pin the literal (`tests/unit/web/coordination/test_identity_authority.py`,
  `tests/unit/web/sessions/test_identity_repository.py:108`). They call
  `retire_identity` directly and keep passing; the retirer's composition gets
  its own test.
- `IdentityRetired` carries no actor ("the actor is the OPERATOR"); the
  route's `_slog.info` line is the only place the administrator is named. A
  typed reason with no author is weak evidence. Out of scope for D3 as ruled;
  raise it rather than widen P1 silently.
- A reason held in component state does not survive a reload. When the
  unfinished-removal notice renders with no held reason, the retry asks for
  one; it never sends none.

**P2. The sole-administrator warning is shown in two places, to everyone.**
`PeopleAccessDialog.tsx:241` shows a permanent `role="status"` banner whenever
`active_human_admin_count === 1`, which is every open on a one-admin
deployment. `AccessSection.tsx:70` shows "If X is the only active
administrator…" on every active person while that count is 1
(`PersonDetail.tsx:193` passes `activeAdminCount === 1`), because the panel
knows how many administrators there are and not who they are.

- Fix: have the person read (or the directory row) say whether this person is
  the sole active administrator, show the warning on that person's Access
  section only, and drop the panel-wide banner.
- This is a backend change too (go/no-go review, 2026-09-21): the wire carries
  only `active_human_admin_count` (`types/people.ts:71`,
  `types/identityAdmin.ts:34`), computed by `_active_human_admin_count`
  (`identity_authority.py:976`). The new field needs its affected Python tests.

**P3. The search hint is not announced.**
`components/ui/Input.tsx:88` renders `hint` in a div with no id, and
`PeopleDirectory.tsx:60` spends `aria-describedby` on the scope paragraph. The
gap is in the shared `Input` and predates the panel; axe does not detect it.

- Fix in `Input`: give the hint an id and merge it with any caller-supplied
  `aria-describedby`. This benefits every `Input` with a hint.
- Verify: a unit test that the input's `aria-describedby` contains both ids.

**P4. Five peer sections use two patterns.**
The intro names "access, roles, approvers, limits and sign-in".
`PersonDetail.tsx` renders Access and Sign-in as `h4` sections with Roles /
Approvers / Usage & limits as tabs between them, so Sign-in moves as the tab
height changes. Confirmation forms also use `h4`, the same level as the
sections that contain them.

- Fix: either move Sign-in above the tablist so the fixed sections are
  together and the tabs end the page, or make all five tabs. Demote
  confirmation headings one level below their section.
- Since the merge, a tab change with typed input asks before it discards
  (`PersonDetail.tsx:204,206`, review finding 8). "Make all five tabs" now puts
  the Access and Sign-in confirmation forms behind that same prompt, which is
  the right behaviour but raises the cost of P5. Moving Sign-in above the
  tablist is the smaller change.

**P5. The leave prompt puts the destructive choice first.**
`PeopleAccessDialog.tsx:230-235`: "Discard changes" (danger) precedes "Keep
editing". The prompt is `role="alertdialog"` without focus containment, and it
renders at the top of the panel body.

Raised from Low after the merge: the prompt used to appear on leaving a person
or closing the panel. It now also appears on every tab change with typed input,
so administrators meet the danger-first, uncontained prompt far more often. The
prompt takes focus (`PeopleAccessDialog.tsx:160`), so it is reached; one Tab
past its two buttons still lands in the form behind it.

- Fix: put the safe action first; while the prompt is open, make the rest of
  the panel body `inert` so Tab stays in the prompt.
- React is 18.3, which does not type `inert` as a boolean prop: copy the idiom
  already in `components/workspace/ComposerWorkspace.tsx`. jsdom does not
  enforce `inert`, so a unit test can assert the attribute only; containment is
  a live-pass check.

### Low

**P6. The Sign-in method filter shows raw provider tokens.**
`PeopleDirectory.tsx:75` lists `oidc`, `entra`, `vanguard`; the same tokens
appear in `SignInSection.tsx` ("signs in through entra"), in
`personDisambiguator`, and in the Add person provider list
(`AddPersonForm.tsx:119`). `service` is both a Sign-in method and a Type.

- Fix: one provider-label map in `peopleFormat.ts`, used by all four sites;
  decide whether `service` belongs in the Sign-in method list at all given the
  Type filter.

**P7. Active, Disabled and Retired badges look the same.**
`admin.css:218`. Only Pending access and Access not set up get a distinct
border. Text differs, so this is a scanning cost, not a WCAG 1.4.1 failure.

- Fix: mute Disabled and Retired (muted text colour plus the existing border)
  so an active roster reads at a glance. Keep the text labels.

**P8. "Copied" never resets.**
`GeneratedPassword.tsx` and `CopyId` in `PersonDetail.tsx`. Reset to idle after
a few seconds.

**P9. Dialog width and pane breakpoint disagree.**
`admin.css:60` sets `width: 1040px` (capped by `.app-dialog` at
`100vw − 32px`); the panes collapse at a 760px viewport (`admin.css:342`).
Between the two, the list column holds at its 260px minimum. Confirm live,
then either raise the breakpoint or switch the pane collapse to a container
query on the dialog.

## Status (2026-09-21)

Implemented on local `release/0.8.1` in two commits: the composer half
(L1–L12) and the People & access half (P1–P8), each with tests that pin the
fix.

Deliberately not done, per this plan's own sequencing:

- **L8, the value clamp and the default-open policy.** Both scroll wrappers
  are keyboard-focusable and named. Clamping long approved values and choosing
  one default-open policy still wait for the live look (short laptop viewport,
  one long prompt approval); `GraphView.test.tsx` pins today's policy
  (Approvals closed, Routing open).
- **P9.** Inferred from the stylesheet; needs a served build of
  `release/0.8.1` before the breakpoint or a container query is chosen.
- **The live pass** (L3/L6 visually, P5 focus containment, which jsdom cannot
  enforce). The People & access merge is still local-only.

Found while implementing, and fixed:

- A fifth raw-provider-token site for P6: the server's `PersonLabel.detail`
  was formatted `"sam.lee · oidc"`. It now carries `provider` as data and the
  panel names it from the one map (`PROVIDER_LABEL`, in `api/people.ts` beside
  `personDisambiguator`, which needs it — not `peopleFormat.ts`).
- P2 moved the warning off a list-level count that every directory refresh
  renewed, so a role write now re-reads the open person as well as notifying
  the directory.
- P1 needed no schema change: `identities.disable_reason` is an unbounded
  nullable `String` with no CHECK on it (measured), so the testcontainer
  selection was not owed. The reason is bounded at 480 characters so that the
  fixed prefix plus the administrator's words fits the audit trail's 512.
- P2's new authority read shifted 48 line pins in
  `tests/unit/architecture/test_session_db_mutation_authority.py` by +13 with
  every fingerprint unchanged; re-pinned mechanically and proved with the
  gate's own drift function.

Open, for John:

- `IdentityRetired` still names no actor, so the deletion reason is audited
  without its author (see P1). Not widened here.
- No CHANGELOG line was written; which release these belong to is not an
  inference from the checkout. `elspeth composer users remove` now REQUIRES
  `--reason`, which is operator-visible.

## Suggested order

1. L3, L4, L10, L12, P6, P8 — small and independent.
2. L1 and L2 — copy and comment cleanup per D1 and D2.
3. L6 with L5 — they change the same panel, and the live session shows they are
   one problem: an optional Apply was the only button beside an unfixable block.
4. L7 — vocabulary; test pins move with it.
5. P3, P5 (raised after the merge), then P1, P2, P4. All on `release/0.8.1`;
   the panel branch no longer exists.
6. L8 and P9 after a live look; L9, L11 as capacity allows.

## Verification

- Frontend unit tests for each touched component (`npm test` scoped to the
  file) and the frontend E2E fixtures that pin decision-panel and Graph tab
  copy (`ab2a8a366` shows where those live).
- Frontend-only changes do not require the Python full suite; L11 (if it adds
  a notice code on the wire), P1 (delete reason on the route) and P2 (sole
  administrator on the person read) do touch the backend and need their
  affected Python tests.
- A live pass against the running composer to confirm L8 and to check L3/L6
  visually. The People & access merge is local only (not pushed, not deployed),
  so P9 and the P5 prompt need a served build of `release/0.8.1` first.
- P1, P4 and P5 touch code that `PeopleAccessDialog.review.test.tsx` now pins
  (the finish-removal retry, the tab-change guard). Run it with each.
