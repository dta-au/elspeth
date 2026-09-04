# UX Design Review — Composer Guided Mode and First-Run Tutorial

**Date:** 2026-09-02
**Reviewed at:** `release/0.8.0` @ `947a3b2c7`, live on the dev deployment
(elspeth.foundryside.dev), Composer model Claude Sonnet 5
**Prompted by:** operator observation that guided mode is "somehow in the zone
of being less useful than freeform (and less intuitive)"
**Method:** multi-competency design review (visual, information architecture,
interaction, accessibility, AI trust stack). Live walk of the first-run tutorial
end to end, a fresh live guided session, an existing guided session parked at
the Transforms step, and a freeform session given the tutorial's own
one-sentence request as the control. Backend behaviour mapped from
`src/elspeth/web/composer/guided/` and `sessions/routes/composer/guided.py`.
Every UI string quoted below was read from the live accessibility tree.
**Companion:** rendered report with screenshots —
https://claude.ai/code/artifact/5c302a58-4deb-4af8-8a6e-d8b993f2d5bd
**Remediation plan:** `docs/plans/2026-09-02-guided-tutorial-remediation.md`

## Verdict: NEEDS WORK

**Counts:** Critical 5 · Major 11 · Minor 6

The operator's instinct is measurable. Building the tutorial's pipeline took
15 gestures in guided mode with no graph on screen until after wiring was
committed. The same request in freeform took 2 messages, with the graph live
beside the chat. Guided mode is not a gentler version of freeform; it is a
stricter one with less to look at.

| Path | Gestures to a reviewable pipeline | Planner runs | Wall clock | Graph visible while deciding |
|---|---|---|---|---|
| Tutorial (guided, locked prompts) | 15 | 2 | 8 min 52 s | No — only after Confirm wiring |
| Freeform, same request + the three page URLs | 2 messages + 5 card actions | 2 turns | 3 min 30 s | Yes — right pane, live |

Wall clock runs from the first build gesture to the pipeline being ready for
review and includes the reviewer's own clicking. Model time alone on the
tutorial (four waits of 40 s, 35 s, 44 s, 80 s plus a 90 s run) is about five
minutes against the welcome screen's "about 3 minutes". Freeform's second turn
was a clarifying question for the scraping contact and reason — the exact two
facts the tutorial pre-bakes into its locked transforms prompt.

## Root cause: the June reframe stopped halfway

This is not a new diagnosis. Epic `elspeth-e7757e5c58` (2026-06-29/30
first-principles UX eval) found guided mode "stacks three competing
interaction models in one column: a wizard, a chat, and a review surface"
with "two input loci, no back-nav, all steering secondary". It planned six
slices:

| Slice | Purpose | Status (2026-09-02) |
|---|---|---|
| A · surface cleanup | vestigial vocabulary, working indicator | shipped (`elspeth-b30e59bfa3`) |
| B · advisor dead-end | surface findings instead of a bare Confirm | shipped (`elspeth-7b0f75e90e`) |
| C · live graph | graph as the verification surface | shipped as a mini graph INSIDE the decision card only (`elspeth-aabb519a49`) |
| D · conversational-primary | demote the widget wizard to an assistant-offered fallback | DEFERRED (`elspeth-cc1f5e49d1`) |
| E · back-navigation | real back transitions, first-class steering | DEFERRED (`elspeth-191f5ffe26`) |
| F · tutorial honesty | tutorial must not be easier than the surface it teaches | DEFERRED (`elspeth-346dd204df`) |

Slice C was additive: it put a third surface into the column without the
subtraction slice D was meant to make. Today one 360 px column carries the
stepper, the decision card with its own graph, the option widget, the chat
box, a revision-scope select, and at the wire step a fourteen-option "Edit
reviewed component" form. Beside it a 1080 px pane reads "No pipeline to
visualise. Start a conversation to build one." through all four steps,
because pre-commit composition state is empty by design.

The backend confirms the shape the UI implies (all citations verified
2026-09-02):

- `ControlSignal.BACK` is declared (`guided/protocol.py:439`) and has zero
  handlers anywhere under `src/elspeth/web/`. There is no back-step; the only
  rewind (`intent_management.py:253`) reaches STEP_2_SINK and explicitly
  raises on the source stage.
- Guided has no node, gate, fork, coalesce or edge authoring surface. Every
  structural change is a whole-pipeline re-plan from prose
  (`guided.py:3172` `requires_planner`).
- A COMPLETED guided session cannot be re-entered (`guided.py:1032-1038`);
  re-entry exists only for `exited_to_freeform`.
- The proposal card's option allowlist (`protocol.py:862`
  `_NODE_OPTION_SUMMARY_ALLOWLIST`) covers `field_mapper` only, so an `llm`
  node shows no prompt and no model before commit.
- The step-2→3 transition is the only one that invokes the planner
  (`guided.py:4915-4919`). The tutorial profile forbids a start intent and the
  live profile takes the user's FIRST chat message as the root intent
  (`stores/sessionStore.ts:3609`) — so the planner is briefed with a source
  description, or with the fallback sentence, never with a goal.
- Freeform can author the same graph with one `set_pipeline` call from a
  palette of 40 tools, up to 16 tool calls per turn.

**One-line version:** guided takes freeform's planner, adds four gates and
three confirmation rounds per stage, hides the graph until after the commit,
and removes the chat at the end. The novice it was built for gets more
ceremony and less feedback than the expert.

## The tutorial, turn by turn

| Stage | Gesture | What the learner sees |
|---|---|---|
| Source | Send | 40 s wait; "I'm reading your message for this guided turn." with a timer |
| Source | Continue | a raw JSON schema block, then "Path: Uploaded sample data" |
| Source | Looks right | a one-column table headed `url`, no rows |
| Source | Finish sources | a one-item list with Edit source |
| Output | Send | 35 s wait; seven settings rows incl. "Collision Policy: auto_increment", "Encoding: utf-8" |
| Output | Continue | "Which fields must appear in the output?" with `url` pre-selected |
| Output | Let source decide | the SECONDARY button; the primary Continue is the wrong answer (I-3) |
| Output | Finish outputs | immediately: 44 s unrequested planner run |
| Transforms | (none) | "A complete pipeline is ready for review. 1 sources · 0 nodes · 3 routes · 1 outputs" |
| Transforms | Send | 80 s wait; second full re-plan; nodes named node-1, node-2, node-3 |
| Transforms | Review wiring | the same components listed a third time; "9 routes — 1 connected, 8 not yet checked" |
| Wire | Confirm wiring | graph appears for the FIRST time, nodes now scrape_page / summarize / cleanup, two with red error badges |
| Review | View prompt, Approve, Acknowledge ×2 | the prompt is readable for the first time; the run starts on the last click |

## Findings

### Information architecture

| # | Finding | Severity | Evidence | Recommendation |
|---|---|---|---|---|
| IA-1 | Verification surface absent while decisions are made: right pane empty through all four steps; graph exists only after Confirm wiring | **Critical** | pre-commit composition state empty by design; the card's mini graph renders from the proposal payload | feed the right-pane graph from the proposal payload from the first reviewed source |
| IA-2 | Three input loci in one column (option widget, chat box, revise controls); wire step adds a 14-option combobox mixing components and routes | **Critical** | slice D deferred | one primary input: chat; widgets offered by the assistant when it cannot resolve intent |
| IA-3 | The first question is the wrong one: "Which data source would you like to use?" / "Describe the source you have". The goal is never asked; that first message becomes the planner's root intent | **Critical** | `sessionStore.ts:3609`; order inversion already tracked as `elspeth-1318049ffe` | ask the goal first, in one sentence; source follows |
| IA-4 | Names change between review and commit: source-1 / node-1 (Fetch) / node-3 (Output) / output-1 become scrape_page / summarize / cleanup / json_output. A transform labelled "Output" beside a real output | **Major** | proposal + wire cards vs post-confirm graph | committed names on review cards; never label a transform "Output" |
| IA-5 | Repetition without progression: components and routes listed in the card, in the graph, in the Routes list, then again at wire with disclosures. Nine routes, three times, before Confirm | **Major** | Transforms and Wire cards | one structural view per stage; the graph IS the list |
| IA-6 | Completed guided session is a dead end: "Pipeline ready" + "Open freeform editor", no chat, no transcript, no way back. Graduation copy sends the learner to "the chat panel" | **Critical** | completion one-way server-side | keep the conversation on a completed session, or convert completion into freeform with the transcript carried over |

### Interaction design

| # | Finding | Severity | Evidence | Recommendation |
|---|---|---|---|---|
| I-1 | Tutorial run fires on the last Acknowledge click. No Run button, no preview. Graduation then says "glance at the graph and the YAML before clicking Run" and "nothing executes without your say-so" | **Critical** | last ack 00:39:49, "Running your pipeline." same second; run turn fires on mount | show graph + Run button; let the learner click it |
| I-2 | Consent after commit: the LLM prompt is not visible on proposal or wire cards (allowlist = field_mapper only; Technical details = cardinality, stable id). It appears as an Approve card only after Confirm wiring and cannot be edited there | **Critical** | `protocol.py:862`; wire-stage Technical details | show prompt + model on the proposal card with an Edit that routes to a component revise |
| I-3 | Primary button is the wrong answer on "Which fields must appear in the output?": Continue has `url` pre-selected; the scripted driver clicks the secondary "Let source decide" and its source comment says that is the designed answer. Nothing on screen says so | **Critical** | `tests/e2e/tutorial-reliability.staging.spec.ts:191-196` | skip the turn when no transforms exist yet, or make pass-through primary with one line of explanation. Read `docs/plans/2026-08-19-invert-guided-sink-field-keep.md` first |
| I-4 | No back navigation: completed ticks are not links, decisions summary has no edit, only rewind is exit to freeform; `back` signal is dead code | **Major** | slice E deferred | deliver slice E; interim: completed ticks open a read-only sheet with "Change this" that forks |
| I-5 | Three or four confirmations per stage for one component, each with a different verb (Send, Continue, Looks right, Finish) | **Major** | ledger above | one decision per stage |
| I-6 | Unrequested planner run on entering Transforms produces a zero-node pass-through headlined "A complete pipeline is ready for review."; Send then re-plans from scratch. Two multi-minute runs for one pipeline; the first headline is false | **Major** | `guided.py:4915-4919`; tutorial forbids a start intent | do not plan without an intent; if the transition must plan, headline it as a sketch and count it per transition in the harness (`elspeth-515096e18c`) |
| I-7 | Wire stage: "9 routes — 1 connected, 8 not yet checked" beside an enabled green Confirm; no explanation; chat placeholder says "Clear pending acknowledgements" with none pending; Send disabled with no reason | **Major** | related `elspeth-e241e05a04` | check routes before offering Confirm, or label the state plainly and make Confirm reflect it |
| I-8 | Assistant explanation scrolls away at decision time: transcript sits above the card; pane auto-scrolls to Continue so "ELSPETH said:" is off-screen when the learner is asked to agree | **Major** | source and output steps | put the one-paragraph rationale inside the decision card above its controls |
| I-9 | "Explain this step" is the only way to ask during the tutorial; none after completion | Minor | — | follows from IA-6 |
| I-10 | Exit to freeform confirms on a session with no work | Minor | fresh session, first click | confirm only when there is something to lose |

### Visual design

| # | Finding | Severity | Evidence | Recommendation |
|---|---|---|---|---|
| V-1 | Proportion inverted: 360 px pane holds everything incl. a six-node graph with edge labels wrapping over edges in 8 px type; 1080 px pane empty | **Major** | Transforms card | same fix as IA-1 |
| V-2 | Controls clip at the pane edge: "Edit reviewed component" combobox/textarea/button (rendered "Edi"); "Custom field" input on the output-fields turn | **Major** | wire stage; output-fields turn | `min-width: 0` on the form; let fields shrink |
| V-3 | "Revision scope" is a native unstyled select flush to the viewport edge, outside the card and the composer frame | **Major** | Transforms step (live session) | move into the proposal card beside the Revise controls it governs |
| V-4 | At 390 px "Exit to freeform" overlaps the stepper (Output tick over the label); stepper wraps to two rows with step 5 hidden under the card | **Major** | mobile walk; related `elspeth-ea0fe53d89` | stack the exit button above the stepper < 640 px; scroll the stepper horizontally |
| V-5 | Five heading-like lines before content on the Transforms card | Minor | — | one heading, one sentence |
| V-6 | Run result table renders markdown literally (`## …`, `**$225,000**`); heading still "Running your pipeline." after "Done. 3 rows returned." | Minor | run turn | render or strip markdown; change the heading on completion |

### Tutorial copy honesty

The frozen script is the machinery canary (ADR-031) and is not at issue. The
framing copy around it has drifted from what the learner actually does:

| Where | Copy | What actually happened |
|---|---|---|
| Welcome | "Then you will choose how you want to work going forward." | no choice is offered; the mode-choice turn was removed |
| Welcome | "In about 3 minutes…" | ~5 min of model time before any human latency |
| Welcome | "This step calls the configured LLM and fetches pages over the network." | shown on the step that does neither |
| Graduation | "…interpreted your one-sentence description." | three locked stage prompts; the learner typed nothing |
| Graduation | "…amend or reject — the same gestures you just practised." | tutorial hides Approve/Reject/Revise (`ProposePipelineTurn.tsx` `isTutorial`) |
| Graduation | "…glance at the graph and the YAML before clicking Run." | run auto-fired; YAML tab disabled throughout the build |
| Graduation | "…choose guided for structured prompts or freeform for a conversational, step-by-step exchange." | reversed |
| Graduation | "…ask in the chat panel." | completed guided session has no chat panel |
| Audit | "…the run has the prompt, response, model details…" | shows a hash, a call count, run id, timestamp, plugin versions; no prompt, no response, no link |
| Audit | "…you can correct it by telling the composer what you meant." | no input on this turn or the next |
| Prompt card | "Showing the stored prompt template as-is — the interpretation slots could not be broken out for this card." | internal apology in the learner's first sight of a prompt |

### Accessibility (WCAG 2.2 AA)

- 1.4.3 Contrast — **Pass** (light-theme muted text ≈ 7:1; mono kicker ≈ 5:1 at 12 px semibold)
- 1.4.10 Reflow — **Partial** (V-2, V-4)
- 2.1.1 Keyboard — **Pass** (every gesture is a button/select/textbox; stepper is status, correctly not focusable)
- 2.4.7 Focus visible — **Pass** (focus-visible rules in base, guided, chat, workspace stylesheets)
- 2.4.11 Focus not obscured — **Not verified** (sticky bottom composer vs a focused Continue at short heights)
- 2.5.7 Dragging — **Not verified** (pane resize separator keyboard alternative)
- 2.5.8 Target size — **Pass**
- 3.2.6 Consistent help — **Fail** ("Explain this step" on every guided card; absent on the completed surface and in freeform)
- 3.3.7 Redundant entry — **Pass**
- 3.3.8 Accessible authentication — n/a
- 4.1.2 Name/role/value — **Pass** (regions, logs, status, aria-current; mini graph has alt; timers aria-hidden)

Universal-access notes: cognitive load is the heaviest cost — engineer-register
content at Standard detail (raw JSON schema, `auto_increment`, `utf-8`,
`Stable ID`, `Cardinality: one → one`), and every transform glossed
"Transforms each incoming item." The detail-level epic `elspeth-cd8abcba3f`
has not reached these cards. Temporal: good (no timeouts, every wait
cancellable, progress persisted). Screen reader: route list + graph alt + wire
list means nine routes are announced three times.

### AI trust stack

- **Legibility — Partial.** Guided shows one status sentence and a timer; freeform shows the tool trail too. The guided planner is the more opaque one for the less expert user.
- **Grounding — Fail.** Prompt not visible before commit; audit turn describes evidence it does not show or link.
- **Steering — Fail.** No back; no per-node edit that is not a full re-plan; no prompt editing on the approval card.
- **Refusal & recovery — Not exercised.** Open P1 `elspeth-1ebd324c2d` (composer declares itself blocked without reaching the advisor).
- **Reversibility — Fail.** Tutorial executes on an Acknowledge click. Live guided and freeform keep a real Run button.
- **Calibration — Fail.** "A complete pipeline is ready for review." over zero nodes; green Confirm beside eight unchecked routes; red error badges for pending reviews.

## Priority recommendations

**Critical (before next release)**
1. Show the graph while deciding (IA-1, V-1).
2. Put a Run button in the tutorial (I-1).
3. Show the prompt before the commit (I-2).
4. Fix the output-fields trap (I-3).
5. Keep the conversation on a completed guided session (IA-6).

**Major (structural)**
6. Finish the reframe (slices D, E, F) or close the epic and stop recommending guided as the default while it is the weaker mode.
7. Ask the goal first (IA-3).
8. One decision per stage (I-5).
9. Stop the empty sketch (I-6).
10. Stable names (IA-4).
11. Layout (V-2, V-3, V-4).
12. Copy (eleven lines).

**Minor**: result-table markdown, completion heading, exit-confirm on empty sessions, stacked headings.

## Testing recommendations

- Keyboard-only tutorial pass at 1280×640 watching for a focused Continue under the sticky composer (2.4.11).
- Screen-reader pass of the Transforms card, counting route announcements.
- Five-user hallway test of the output-fields turn, no coaching (prediction: four of five click Continue).
- Tutorial harness: count provider calls per transition and record the step-2 finish sketch as its own transition.
- Mobile pass at 390 and 360 px after the header fix.

## Related tracker items

`elspeth-e7757e5c58` (reframe epic, D/E/F deferred) · `elspeth-1318049ffe`
(step order inverted) · `elspeth-e241e05a04` (Confirm light stage-1 only) ·
`elspeth-cd8abcba3f` (detail level epic) · `elspeth-ea0fe53d89` (mobile rail
focus order) · `elspeth-1ebd324c2d` (blocked without advisor) ·
`elspeth-515096e18c` (harness counts per walk not per transition) ·
`elspeth-12fef8ddff` (per-step accept/reject) · `elspeth-7ddba54edb` (no undo
on proposal rejection) · `elspeth-00ed60d57e` (readiness during guided).

## Review artefacts

Screenshots: `.playwright-mcp/review-*.png` in the session checkout
(gitignored). Two dev sessions were created on the box by the walk: a
first-run tutorial rerun and "Session — 2 Sep 2026 (2)"; both are ordinary
dev data and can be archived.
