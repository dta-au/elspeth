# Composer advisor block: root-cause diagnosis — 2026-09-22

Subject: bug report "Composer hides actionable advisor findings and traps
explanatory turns in repair loops" (session `6990d39f`, requests `fc112a2f`,
`6ebd025d`, revision `971a597b6`).

Method: systematic debugging, Phase 1 to 3 only. No code was changed and no
fix is proposed as a patch. The observed session lives on a deployment this
checkout cannot reach, so nothing here was reproduced. Every finding is a
code-path reading at `971a597b6` (equal to HEAD when read) set against the
call sequence the report itself records. Where a conclusion depends on a fact
only that server holds, it is marked **OPEN** and the question is listed at
the end.

## Verdict on the report's four root causes

| # | Report's claim | Verdict |
|---|----------------|---------|
| 1 | FLAGGED is rendered with no actionable reason | **Confirmed**, and understated: the model's own reply is deleted too, not only the advisor's findings |
| 2 | Planner failed to turn an unsupported topology into a capability explanation | **Not established.** The report's call order is consistent with the planner having explained first and the gate having ordered the destructive edits. **OPEN Q1** |
| 3 | Explain-only turn was misclassified as authoring | **Refuted as framed.** There is no intent classifier on a non-empty pipeline, and the model did answer. The gate ran on an unchanged graph and demanded mutations |
| 4 | Repair success is measured as mutation | **Confirmed**, and it is stronger than that: the injected repair message demands mutation |

## What the code does

### The gate is keyed to session state, not to what the turn authored

`_evaluate_terminal_no_tool_advisor_gate` (`service.py:6539`) runs on every
terminal no-tool reply. It falls through only when the pipeline is
structurally empty, the pass budget is spent, or there are genuine orphaned
review sites (`:6598-6611`). `mutation_success_seen` is a parameter of
`_try_terminate_no_tools` (`:5979`) and is not read anywhere between `:5960`
and `:6200`. Nothing asks whether this turn changed the pipeline.

Consequence: once a session's state is one the advisor will flag, every later
turn meets the gate again, including a turn that is only a question.

### `intent_is_explicit_mutation: null` means "not evaluated"

`service.py:3985-3993`: the mutation-intent classifier runs only when the
state is structurally empty, where it chooses planner versus compose loop.
The comment says so directly: "None in the log means 'not evaluated'". On a
non-empty pipeline there is no explain/mutate classification of any kind, so
the telemetry value in the report is not evidence of a misclassification.

### The model answered; the gate caused the edits

The report's sequence for `6ebd025d` is: planner call, finish reason `stop`,
970 completion tokens; then advisor pass 1 FLAGGED; then three tool-call
repair turns. `stop` means the call carried no tool calls. A 970-token
no-tool reply to "what does it mean, and what are my options?" is an answer.
The composer skill supports this reading: its stop rules permit an
informational reply (`pipeline_composer.md`, stop rule 5) and a named-gap
refusal for an unsupported shape (stop rule 3), and its router reserves
explain-only responses for turns where the user asks for explanation
(`:71-72`).

The gate then reviewed the unchanged graph, returned FLAGGED, and appended
this to the model's context (`service.py:6805-6817`, `:10664-10683`):

> Resolving these findings requires pipeline MUTATIONS via tool calls ...
> Re-reading state ... is not a fix and wastes this repair pass; if no
> mutation can address a finding, say what blocks you instead. Fix the
> findings via tool calls. The end user has NOT seen these findings; your
> final reply ... must state only the outcome — never reference, quote, or
> rebut the advisor.

The A/B gate edit on the explain turn followed that instruction. It was not
the planner misreading a question.

The 970-token answer is unrecoverable at this revision: `ComposerLLMCall` has
no response-text field, and a no-tool reply answered by a repair gate is
neither appended to `llm_messages` nor persisted (case 1 of
`2026-09-22-composer-reply-withholding-review.md`). **OPEN Q2** confirms this
on the live rows.

### The escape hatch leads into the deleter

On the skill the user asked for: the `debug-generation` decision tree (system
message, few-shot examples, output format, temperature) has nothing to act on
here. Every call returned `success` with a normal finish reason, and the
skill text already permits the right behaviour. The LLM-behaviour defect is a
contradictory pair of backend-authored instructions, not model quality.

The injected message offers one exit: "say what blocks you instead". Trace it.
The model replies with no tool calls. `_try_terminate_no_tools` runs the gate
again. The advisor never reads that reply: `_build_checkpoint_arguments`
(`:8193-8316`) takes `state`, `user_message` and the review state, and the
word "assistant" does not occur in it. The clause is addressed to a reader
that does not exist. Pass 2 instead receives byte-identical evidence, the
prior finding as `recent_errors` (`:8261-8267`) and
`attempted_actions = ["No successful pipeline mutation occurred since the
prior advisor pass."]` (`:8274`), which leans it toward FLAGGED again.
`is_last_pass` is true under the default
`composer_advisor_checkpoint_max_passes=2`, so `terminal_block` fires
(`:6676-6678`), `_advisor_blocked_result` withholds the prose
(`raw_assistant_content=""`, `:6736-6738`), and the user receives the fixed
notice from `:10886-10889`.

So the only place the model is told it may explain a capability limit is the
place where its explanation is certain to be destroyed. This is the central
LLM-behaviour finding: two pieces of backend-authored prompt and policy
contradict each other. The model cannot satisfy both, and either choice
produces the outcome the user saw.

### `on_error: discard` was forced, not chosen

The engine parser accepts three forms: a sink name, `discard`, or omitted
(`core/config.py:857-860`, `:1514-1516`). The composer tool surface does not
offer the third. `upsert_node` default-fills
`validated.on_error or "discard"` for transforms and aggregations
(`tools/transforms.py:793`), and removing an `on_error` edge sets the node's
`on_error` to `None` (`:1015-1017`), which elspeth-0aace271b4 (status
`fixing`) says the composer validator reports as `transform_missing_on_error`.
That ticket's claim was not re-verified here. On that reading, once "remove
the failure outputs" is carried out, `discard` is the only value the composer
will accept, so the report's "silently degraded handling to `discard`" is a
tool-layer property, not a planner judgement. **OPEN Q3** settles whether the
model passed the value or the tool layer supplied it.

**OPEN:** the report's alternative "set LLM failures to halt the run visibly".
No halt value exists for `on_error`. What an omitted `on_error` does at
runtime was not read, and no other mechanism was looked for. Keep it out of
user-facing copy and tests until someone has.

### The coalesce limit is not taught

Neither skill file tells the model that a coalesce has no `on_error` route.
`pipeline_capabilities.md:169-171` states it for collectors only. The
instrument was controlled: the same search matched those collector lines. The
model can learn the coalesce rule only from the `state.py:3362` rejection
after it has tried the edge.

### The unmerged withholding branch does not fix this incident

`fix/composer-reply-withholding` decides withholding by
`prose_withheld = advisor_repair_context_introduced` (worktree
`service.py:8500`). In both observed requests pass 1 FLAGGED with
repair-continue, so an injection occurred, so the flag is true, so the final
reply is still withheld. The branch adds a `composer_withheld_reply` audit row
for that reply, and its case 1 fix keeps the first reply in model context and
recoverable. Neither reaches the user. Merging it does not close this bug.

## Two hypotheses for `fc112a2f`, and the fact that separates them

The report lists: initial planner call `stop`, 440 tokens; advisor pass 1
FLAGGED; five tool-call repair turns; final reply; advisor pass 2 FLAGGED.

- **H1 (gate-induced damage).** The 440-token first call was prose with no
  mutation, plausibly the capability explanation the report says was missing.
  The advisor compared the user's request ("remove them and add the error
  container at the coalesce") with a pipeline that still had failure sinks,
  found a visible mismatch, and flagged. The injection demanded mutations. The
  model removed the sinks, set the only legal `on_error`, could not add a
  coalesce error route, and pass 2 flagged the half that cannot be built.
  Under H1 the destructive edits are the gate's doing and root cause 2 is
  false.
- **H2 (planner-induced damage).** Tool-call turns preceded the 440-token
  reply and the report's list omits them. The planner removed the sinks on its
  own initiative and the advisor flagged the result. Under H2 root cause 2
  stands.

The five repair calls match the four reported mutations plus a preview, which
favours H1, but that is an inference. **OPEN Q1** decides it: the state version
at the moment of advisor pass 1.

## Systems view

**An absorbing state.** The flaggable condition is a stock held in session
state. The gate is a balancing loop (detect, repair, re-check) whose single
actuator is "the model mutates". When the condition needs a user decision
among different semantics, the actuator cannot reduce the stock. Every turn
re-enters the loop, spends both passes, and exits through the withholding
path. The user, the only actor able to resolve it, receives no information
from the loop. In Meadows' terms this is a missing information flow (level 6),
not a parameter problem: raising the pass budget would buy more unwanted
mutations.

**Fixes that fail.** elspeth-71617f1d21 added the mutation-expectation clause
because session `2e0c8ea3` spent both repair turns on lookups. That fix is
what turned a question into graph edits here. Each clause was a correct local
response; together they leave the model no compliant move.

**Goal conflict, already priced.** The withholding exists to serve R2-F13 (raw
advisor findings never reach the user). Section C.2 of
`2026-09-22-reply-withholding-rulings-systems.md` (on the branch) asks John to
rule on that goal and asks for incident counts first. This session is such an
incident: a user blocked twice, with the model's answer deleted both times,
while the same findings' influence reached the pipeline through tool calls.

**Rule beating, as a constraint on any fix.** A model given a new exit will use
it to satisfy the letter of the gate (section A.4 of the same review). A
"this turn is explain-only" exit the model can claim for itself will be
claimed on authoring turns too.

## Constraints a fix must respect

These are observations, not a design.

- A server-side classifier of the user's text that skips the gate would be
  routing on user intent. A deterministic check such as "state is unchanged
  since the last terminal block" is not. The distinction matters under the
  composer invariants and should be ruled on before code is written.
- The report's fix 3 ("validate the requested repair is expressible") needs
  the same care: the server may validate and reject what the planner produced,
  but must not interpret the user's request in the planner's place.
- The report's fix 1 (structured findings with backend-owned categories)
  implies the advisor emits a code the backend maps to fixed copy. That is a
  change to the advisor's output contract and to R2-F13's scope, so it is a
  ruling, not a patch.
- Regression test 1 in the report asserts the planner must not remove sinks
  before explaining. If H1 holds, the actor under test is the gate's
  injection, not the planner.

## Rulings (John, 2026-09-22)

1. **A turn that changes no pipeline state does not need the advisor gate.**
   Deterministic on state, not on the user's text, so it is not routing.
2. **Advisor findings may reach the user**, formatted as *header + note*: a
   backend-validated header (category from a closed list; affected step names
   checked against the real pipeline) above the advisor's own words in a
   labelled, plain-text, length-capped block ("Reviewer's note (AI-generated,
   unverified)"). Home: the chat decision panel, per the 2026-09-13 placement
   ruling. This narrows R2-F13 and is the explicit go-ahead that ruling
   required.
3. **The composer's own reply is shown on a blocked turn.** With the finding
   visible there is nothing hidden for the reply to leak. The injected
   "never reference, quote, or rebut the advisor" clause must be reworded to
   match.

Noted while grounding ruling 2: the decision panel's blocker row already
offers "Ask the composer about this", which drafts the very question that was
trapped in `6ebd025d`. The product generates the explain-only turn itself.

## Open questions for the agent with server access

1. **`fc112a2f`, ordering.** The full ordered list of LLM calls with finish
   reason and tool names, and the composition state version before the first
   call and at advisor pass 1. Was the state unchanged when pass 1 ran?
2. **`6ebd025d`, persistence.** Is there any persisted assistant row holding
   the first 970-token reply, or only the fixed "applying a pipeline
   correction" status rows and the terminal notice?
3. **`on_error` provenance.** In the `patch_node_options` or `upsert_node`
   tool rows of `fc112a2f`, do the model's arguments carry
   `on_error: "discard"`, or is it absent from the arguments and present in
   the resulting state?
4. **The unrelated repair.** The state-version diff across `6ebd025d`
   (report says the A/B gate changed, the graph went invalid, then was
   repaired). Is the final v28 identical to the pre-turn state apart from the
   gate, or did the question leave a lasting change?
5. **`AdvisorTerminalPublication` rows** for both requests: `reason`,
   `preflight_shape`, and the advisor `user_message` excerpt length on the
   explain turn, to confirm the advisor was judging the question text against
   the pipeline.

## Not measured

- Advisor findings text for any pass. It is not persisted; the claim that pass
  1 of `fc112a2f` flagged a request/pipeline mismatch is inference.
- How often an unchanged-graph turn is flagged in production.
  `AdvisorTerminalPublication` rows joined to a zero version delta would count
  it.
- Whether `gpt-5.6-sol-datazone` differs from the locally served models in how
  it treats the injected clauses.
