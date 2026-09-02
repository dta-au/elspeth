# Guided Mode + Tutorial Remediation Plan

**Date:** 2026-09-02
**Source:** `docs/plans/2026-09-02-guided-tutorial-design-review.md` (verdict
NEEDS WORK — Critical 5 · Major 11 · Minor 6)
**Tracker:** bug `elspeth-1dc2f92c35` (the issue), blocked by milestone
`elspeth-7578b41719` (this plan, 6 phases, 24 steps). Every step below is a
filigree step under that milestone; claim with `work_start` before touching
code.
**Base:** `release/0.8.0` @ `947a3b2c7`

## Goal

Guided mode stops being the weaker mode. Re-measured with the review's ledger
on the tutorial's own scenario:

| Measure | Today | Target |
|---|---|---|
| Gestures to a reviewable pipeline | 15 | ≤ 7 |
| Planner runs | 2 | 1 |
| Graph visible while deciding | after Confirm wiring | from the first reviewed source |
| Run started by | the last Acknowledge click | a Run button the learner clicks |
| Completed guided session | no chat, no transcript | conversation available |

## Constraints that bound every step

- **The LLM stays the author.** No server-authored sketch, recipe, router or
  fast path reaches the user as a proposal (AGENTS.md composer invariant 1).
  Removing the empty starting sketch (2.2) removes a planner call; it does
  not replace one with server logic.
- **No tutorial-special paths** (ADR-031, invariant 2). Every fix lands on the
  general guided surface and the tutorial inherits it. The frozen tutorial
  script is untouched; only the chrome and copy around it change. Any latency
  or cost change on the rootless/tutorial entry path gets per-transition
  provider-call scrutiny (operator ruling 2026-09-02, AGENTS.md).
- **Verification surface is the graph, not sample output** (epic
  `elspeth-e7757e5c58` D1). No source rows are read to draw anything.
- **Whole-tree gates** (CONTRIBUTING.md): no new `getattr`/`hasattr`; trust-tier
  corpus captured before and after each lane and must not grow; new TSX class
  names need real stylesheet rules; Zustand-touching tests reset the store.
- **Sink field semantics were adjudicated** in
  `docs/plans/2026-08-19-invert-guided-sink-field-keep.md` and
  `2026-08-19-sink-field-keep-executable.md`. Step 1.4 reads both first.

## Phase 0 — Decide the shape and baseline the ledger (`elspeth-07ed2fd0ad`)

| Step | Ticket | Owner |
|---|---|---|
| 0.1 Operator decision: reopen reframe slices D/E/F (`elspeth-e7757e5c58`) or close that epic and change the preferences default | `elspeth-f2a7d86709` | John |
| 0.2 Baseline the tutorial ledger per TRANSITION in the harness (gestures, planner runs, provider calls, wall clock) | `elspeth-f191ba494a` | agent |

0.1 gates only 1.5, 2.4 and 2.5. Everything else proceeds. 0.2 closes the
granularity gap in `elspeth-515096e18c` and is the "before" for Phase 5.

## Phase 1 — Critical fixes, independent of the shape decision (`elspeth-e32b397f37`)

| Step | Finding | Ticket | Notes |
|---|---|---|---|
| 1.1 Right-pane graph from the guided proposal payload during steps 1–4; retire the in-card mini graph | IA-1, V-1 | `elspeth-9f0873426a` | Frontend. Project `ProposePipelinePayload` (and the reviewed source/output before a proposal exists) into the Pipeline artifact pane. Blocks 2.4 exactly as C blocked D. |
| 1.2 Tutorial shows the graph and an explicit Run button after acknowledgements; the run never auto-fires | I-1 | `elspeth-e84782af33` | `TutorialTurn4Run` fires `POST /tutorial/run` on mount today. Keep cancel + resume semantics. Blocks 3.2. |
| 1.3 Show the llm node's prompt and model on the proposal card before commit, with an Edit that routes to a component revise | I-2 | `elspeth-0b62be172f` | Backend allowlist `_NODE_OPTION_SUMMARY_ALLOWLIST` (`guided/protocol.py:862`) + `ProposePipelineTurn` / `WireStageTurn`. Reuse the approval card's redaction boundary. |
| 1.4 Remove the output-fields trap: skip the multi-select turn when no transforms exist yet, or make pass-through primary with one line of explanation | I-3 | `elspeth-023c07ee2a` | Read the 08-19 sink-field-keep plans first. Preferred: emit the turn only when reviewed producers can satisfy it, else defer past the transforms proposal. |
| 1.5 Completed guided session keeps its conversation | IA-6 | `elspeth-986801d218` | Blocked by 0.1. Guided-primary → keep the guided chat channel on a completed session (step-4 solver must answer questions about the committed pipeline). Freeform-default → convert completion into freeform carrying `chat_history` as context (freeform reads only the messages table today, `prompts.py:613-618`). |

## Phase 2 — Structural: one input, one decision per stage, a real goal (`elspeth-ae2dd40370`)

| Step | Finding | Ticket | Notes |
|---|---|---|---|
| 2.1 Ask the goal first: the live start intent is one sentence about the outcome; the source question follows | IA-3 | `elspeth-378cfa0e18` | `sessionStore.ts:3609` sends the first chat message as the start intent while the placeholder asks for the source. Goal-first half of `elspeth-1318049ffe`. |
| 2.2 No planner run without an intent: stop the empty starting sketch on the step-2 finish, or headline it honestly and count it | I-6 | `elspeth-13579d1110` | Blocked by 2.1. `guided.py:4915-4919` is the only planner invocation. Never headline a 0-node passthrough as "complete". |
| 2.3 One decision per stage: collapse schema form + column confirmation + component list into one card with one Continue and one Edit | I-5 | `elspeth-0ecd377e7c` | Turn types stay in the protocol for the Edit path; the default walk stops visiting them separately. |
| 2.4 Slice D: chat is the one primary input; option widgets become assistant-offered fallbacks | IA-2 | `elspeth-74e92c327f` | Blocked by 1.1. Was `elspeth-cc1f5e49d1`. Crown-jewel guard: `chat_solver` / planner contracts unchanged. |
| 2.5 Slice E: back navigation; completed ticks open that stage's decision; "Change this" forks; `ControlSignal.BACK` gets a handler | I-4 | `elspeth-f2a8550b3d` | Was `elspeth-191f5ffe26`. Design first; fork-from-here over mutate-in-place. Interim: read-only sheet per completed tick. |
| 2.6 Stable component names on review cards; kill the "Transforms each incoming item." gloss and the triplicated route lists | IA-4, IA-5 | `elspeth-6b48cb4c31` | Never label a transform "Output". Routes once per stage; text list is the accessible alternative only. |
| 2.7 Wire-stage calibration: check routes before offering Confirm, or label "not yet checked" plainly and make Confirm reflect it; fix the stale placeholder | I-7 | `elspeth-e4c2ebb697` | Related `elspeth-e241e05a04`. |

## Phase 3 — Tutorial honesty (slice F) and copy (`elspeth-3928586bb4`)

| Step | Finding | Ticket | Notes |
|---|---|---|---|
| 3.1 Copy: the eleven drifted lines (welcome, graduation, audit, prompt card) | copy table in the review | `elspeth-2f7709c523` | Write each line against landed behaviour, not the plan. |
| 3.2 Slice F: stop hiding Approve/Reject/Revise from the tutorial's proposal card | copy: "the same gestures you just practised" | `elspeth-18cebcf219` | Blocked by 1.2. Was `elspeth-346dd204df`. Coach, do not remove controls. |
| 3.3 Audit story links to the recorded prompt and response, not only a hash and a call count | Grounding | `elspeth-537ce91041` | One click from the card to the run record. |
| 3.4 The assistant's rationale renders inside the decision card above its controls | I-8 | `elspeth-a624685709` | Reuse `guidedRationale.ts`. |

## Phase 4 — Layout, register and polish (`elspeth-13c52b3212`)

| Step | Finding | Ticket | Notes |
|---|---|---|---|
| 4.1 Revise form / custom-field input stop clipping; Revision scope moves into the proposal card; mobile header stacks below 640 px | V-2, V-3, V-4 | `elspeth-edb09ab53d` | Add a 360/390 px Playwright screenshot pin. Related `elspeth-ea0fe53d89`. |
| 4.2 Detail-level gating on guided decision cards (raw JSON schema, collision policy, encoding, mode, stable id, cardinality) | cognitive | `elspeth-5052aa9de8` | `useShowAdvanced()` idiom from `elspeth-cd8abcba3f`; plain summary in place, no hints. |
| 4.3 Minor batch: result-table markdown, completion heading, exit-confirm on empty sessions, stacked headings | V-5, V-6, I-10 | `elspeth-7ab0de85c8` | |
| 4.4 Accessibility verification: 2.4.11, 2.5.7, route list announced once | a11y | `elspeth-e2bbff21eb` | 3.2.6 resolves with 1.5. |

## Phase 5 — Verify against the ledger (`elspeth-3ac264e311`)

| Step | Ticket | Notes |
|---|---|---|
| 5.1 Re-run the tutorial ledger and the freeform control against the Phase 0 baseline | `elspeth-b56bc96c73` | Blocked by 0.2. Attach the before/after table to the milestone and to `elspeth-1dc2f92c35`. |
| 5.2 Five-user hallway test of the output-fields turn and the first guided question, no coaching | `elspeth-07a4518a59` | Prediction on record: 4 of 5 click Continue on the old turn. |

## Sequencing

```
0.1 (John) ─────────────┬──────────────► 1.5
0.2 (baseline) ─────────┼──────────────────────────────────► 5.1
1.1 graph in pane ──────┼──► 2.4 slice D
1.2 tutorial Run button ┼──► 3.2 slice F
1.3 prompt before commit│
1.4 fields trap         │
2.1 goal first ─────────┴──► 2.2 no empty sketch
2.3, 2.5, 2.6, 2.7, 3.1, 3.3, 3.4, 4.x  — independent
```

Phase 1 can be dispatched as five lanes today. Phase 2's 2.3 and 2.6/2.7
touch the same turn components as 1.3 and should follow it rather than run
beside it (shared-file WIP; see the lane discipline in AGENTS.md).

## Done means

- The ledger in Phase 5 meets the target table above.
- No new trust-tier findings across the before/after corpus captures.
- The tutorial harness counts provider calls per transition and the step-2
  finish no longer appears as a planner run.
- Every copy line in the review's honesty table describes something the
  learner actually did.
