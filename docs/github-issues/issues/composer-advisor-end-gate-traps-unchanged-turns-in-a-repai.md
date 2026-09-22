---
title: Composer advisor sign-off gate traps unchanged turns and withholds the reason
labels: [area/composer, area/web, type/bug]
---

Asking the composer what a validation block means can leave the session stuck. The advisor sign-off gate runs on every no-tool terminal turn against session state without checking whether the turn changed anything, so a question about a block is itself flagged — and the answer that would tell the user what to do is the thing withheld.

## What happens

Observed on a deployment running `971a597b6`; not reproduced locally.

A user asked what a block meant and what their options were. The model answered — the first provider call finished with `stop`, 970 tokens — and the turn was then flagged against an unchanged graph. The gate injected `_ADVISOR_MUTATION_EXPECTATION_CLAUSE` (`src/elspeth/web/composer/service.py:6964`, defined at `service.py:11155`), which instructs the model to edit the pipeline. The clause's only exit is to state what is blocking instead; taking it produces a second advisor pass over identical evidence, which is the last pass, and the prose is withheld. The user is told they are blocked and not told why.

## Why

The gate's condition is the session's validation state, not the turn's effect on it. A turn that mutates nothing re-enters the gate with exactly the evidence that flagged the previous turn, so no amount of answering can clear it.

In the recorded turn `intent_is_explicit_mutation` is `null`, which means not evaluated rather than false: the classifier runs only on an empty pipeline (`src/elspeth/web/composer/service.py:4066`).

A related change to reply withholding does not resolve this. An injection has occurred, so `prose_withheld` remains true (`compose_advisor_signoff_flagged_red_message`, `src/elspeth/web/composer/no_tool_policy.py:1082`) and the reply is still suppressed.

## Impact

Any session where a user asks a question while validation is unhappy — which is the natural next message for someone who has just been blocked, and worst for a new user. The composer answers correctly and the answer is discarded.

## Fix

Three behaviours, none of which weakens the gate on turns that do mutate state:

1. A turn that changes no pipeline state skips the gate. The test is the state version compared against the version at the start of the turn, never the user's text — text is not a reliable signal of intent.
2. Advisor findings reach the user: a backend-validated header drawn from a closed category, with step ids checked against state, plus the advisor's own words in a labelled, length-capped plain-text note in the chat decision panel (`DecisionPanel`).
3. The composer's reply is published on a blocked turn rather than replaced.

## Note

One question is unresolved and it decides what a regression test should target. The state version at the first advisor pass of the reported request determines whether the gate or the planner removed the failure sinks; until that is settled, a test written against the gate may be aimed at the wrong actor.
