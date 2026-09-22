---
title: Composer advisor sign-off gate traps unchanged turns and withholds the reason
labels: [area/composer, area/web, type/bug]
---

Asking the composer what a validation block means can leave the session stuck. A reviewer pass gates every turn that ends without a tool call, and it judges the session's current state rather than what the turn changed — so a question about a block is itself flagged, and the answer that would tell the user what to do is the thing withheld.

## Where to start

The Web Composer is ELSPETH's conversational authoring surface: a user talks to a model (the planner) which builds a pipeline by calling tools. Two terms used below: the **advisor** is a separate reviewer model pass that runs at the end of a turn and can return a blocking verdict, and a **no-tool terminal turn** is one where the planner replies in prose and calls no tool, which is where the composer decides the turn is over and runs that gate.

The code: `src/elspeth/web/composer/service.py` holds the turn loop and the gate (`_TerminalNoToolAdvisorGateOutcome`, around line 6942); `src/elspeth/web/composer/no_tool_policy.py` composes the messages a blocked turn produces; the chat surface that would display findings is `src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx`.

## What happens

Observed on a deployment running commit `971a597b6`; not reproduced locally.

A user asked what a block meant and what their options were. The planner answered — the first provider call finished with `stop`, 970 tokens — and the turn was then flagged against a graph it had not touched. The gate appended an instruction to the model's own message history (`service.py:6958-6969`) carrying `_ADVISOR_MUTATION_EXPECTATION_CLAUSE` (defined at `service.py:11155`), which directs the model to edit the pipeline. That clause's only exit is to state what is blocking instead; taking it produces a second advisor pass over identical evidence, which is the last pass available, and the prose is then withheld. The user is told they are blocked, and not told why.

## Why

The gate's input is the session's validation state, not the turn's effect on it. A turn that mutates nothing re-enters the gate with exactly the evidence that flagged the previous turn, so there is no exit the model can reach by behaving correctly.

Two supporting details from the recorded session:

- `intent_is_explicit_mutation` is `null`, which means not evaluated rather than false. The classifier that sets it runs only when the pipeline is empty (`service.py:4066`), so on a populated pipeline nothing distinguishes "the user asked a question" from "the user asked for a change".
- A related change to reply withholding does not resolve this by itself. Because an injection did occur, `prose_withheld` stays true (`compose_advisor_signoff_flagged_red_message`, `no_tool_policy.py:1082`) and the reply is still suppressed.

## Impact

Any session where a user asks a question while validation is unhappy — the natural next message from someone who has just been blocked, and worst for a first-time user.

## Fix

Three behaviours, none of which weakens the gate on a turn that does change the pipeline.

1. **Skip the gate when the turn changed no pipeline state.** Compare the state version at the end of the turn with the version captured at its start. Do not branch on the user's message text: text is not a reliable signal of intent, and the classifier above already shows that failure mode.
2. **Let advisor findings reach the user.** A header built by the backend from a closed set of categories, with any step ids checked against the actual state before display, plus the advisor's own words in a clearly labelled, length-capped plain-text note in the chat decision panel. The advisor's text is model output, so it is rendered as data and never as instructions.
3. **Publish the composer's reply on a blocked turn** instead of replacing it.

Two things must both be true to call it done: a question asked against an unchanged, invalid pipeline is answered and published with the block explained; and a turn that does mutate the pipeline into an invalid state is still blocked. The second matters as much as the first, because it is what the gate exists for.

## Size

Item 1 is well specified and small — a comparison and an early return, with tests around both branches. Items 2 and 3 are larger and touch both backend and frontend, and item 2 cannot start until the closed vocabulary for the header is agreed, which is a design decision rather than an implementation detail. The three are separable; item 1 alone removes the trap.

## Note — unresolved, and it decides a test

Which component removed the failure sinks in the reported session has not been settled. The state version at the first advisor pass would distinguish the gate from the planner, and that determines which component a regression test should target. Settle it before writing that test, not after.
