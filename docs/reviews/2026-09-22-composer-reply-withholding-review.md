# Composer reply withholding review — 2026-09-22

Question asked: where does ELSPETH remove the composer model's reply before it
reaches the user?

Single direct read of the reply path at `release/0.8.1` HEAD `7d981d6cf`. HEAD
advanced from `fa26f34b8` during the read; `git diff --stat` over every file
cited below is empty between the two, so the line numbers hold. No application
changes were made. Nothing here was reproduced against a live session: every
finding is a code-path reading, and frequency in production is **not measured**
(see "Not measured").

## The defended path, for contrast

`no_tool_finalize.finalize_no_tool_response` is augmentation-only. Every exit
appends a trusted suffix to the model's prose and
`_enforce_augmentation_prefix_invariant` requires
`message.startswith(raw_assistant_content)`. Its docstring records why: the
earlier full-replacement synthesizer "hid the model's actual output from both
the user and ... the model itself" (elspeth-861b0c58f5, elspeth-9cfbad6901).
The same holds for `_proof_repair_blocked_result` (`service.py:3496`) and the
orphaned-interpretation result (`service.py:6353`).

Every case below is an exit that does not pass through that invariant, or that
satisfies it vacuously.

## Summary

| # | Site | Trigger | Withholding disclosed? | Prose recoverable? |
|---|------|---------|------------------------|--------------------|
| 1 | Compose-loop repair gates | model's no-tool reply trips a repair gate | No | No |
| 2 | Planner prose nudge | planner replies in text without `DECLINE:` | No | No |
| 3 | Advisor-cohort tool-call turns | any tool turn after advisor context entered | No (a status line is shown instead) | No |
| 4 | Advisor END-gate block | sign-off flagged / unavailable / malformed | Yes | No |
| 5 | Advisor repair terminal | turn ends after advisor context entered | Yes | No |
| 6 | Planner staging | proposal staged | n/a (fixed copy) | No |
| 7 | Guided scaffold-leak guard | reply contains `<tool_call` etc. | Yes | No |
| 8 | Guided shape/commit rejection | tool arguments rejected at repair exhaustion | Yes | No |

"Recoverable" means present in any persisted row. `ComposerLLMCall`
(`contracts/composer_llm_audit.py:148-178`) stores request hashes, token
counts and `reasoning_content`, and has **no response-text field**, so prose
that is not written to a chat row exists nowhere.

## Silent removals (no disclosure, not recoverable)

### 1. A no-tool reply that trips a repair gate is discarded

`_try_terminate_no_tools` (`service.py:5949`) runs when the model stops calling
tools, i.e. when it has written what it believes is its final reply. Six gates
can answer that reply with a synthetic `role: "user"` message and
`action="continue"`:

- pre-state interpretation-review repair (`:6003`)
- pending interpretation-review repair (`:6047`)
- empty-state uploaded-blob repair (`:6055`)
- proof repair (`:6082`, injects "Do not respond to the user yet" at `:3490`)
- runtime-preflight repair (`:6119`; the second call site at `:5651` follows a
  tool-call turn whose assistant row is already appended and persisted, so it
  drops nothing)
- advisor END gate, blocking verdict with repair-continue (`:6789`)

In none of them is the model's reply appended to `llm_messages` or persisted.
Measured: across `src/elspeth/web/composer/` the only `"role": "assistant"`
constructions are `tool_batch.py:756` (the tool-call turn),
`pipeline_planner.py:1787` (planner-internal) and three guided-lane sites; all
eleven `llm_messages.append` sites in `service.py` were read and carry role
`user` or `tool`. So the reply is lost to the user **and** to the model: the
next provider call shows a user message answering an assistant turn that is
not in the context. Up to `_MAX_REPAIR_TURNS` plus the advisor pass budget of
these can occur per user message.

Sibling that is not a repair gate: the model writes its final reply, the END
gate's masked re-validation finds the compose deadline spent and raises
`_AdvisorCheckpointComposeDeadlineExpired` (`:6707`), which becomes
`ComposerConvergenceError.capture(...)` (`:6165`). The user receives the
timeout envelope. `ComposerConvergenceError` carries no assistant-prose field,
so the completed reply is gone.

Documented rationale: none for dropping the prose. The gates exist to stop a
false completion claim reaching the user, which the finalizer already handles
by augmentation.

### 2. The planner discards any text reply that lacks `DECLINE:`

On a structurally empty pipeline with an explicit build request the turn is
routed to the planner (`service.py:3990`). There,
`_parse_response_tool_calls` (`pipeline_planner.py:1625-1648`) admits a
no-tool-call text reply only when it leads with the case-sensitive `DECLINE:`
marker, and only when `prose_decline_eligible`. Anything else raises
`PROSE_REPLY`; the loop (`:4218-4245`) appends "Your previous reply called no
tool..." and retries, `_PROSE_NUDGE_BUDGET = 2` times, then goes to the escape
hatch or fails `MALFORMED_RESPONSE`. A marker with an empty body also routes
to the nudge lane (`_marked_decline_body`, `:1594`).

Channels the planner does have for reaching the user: the `DECLINE:` body
(published verbatim, `service.py:5183-5205`), and whatever the `pipeline`
argument can express. The terminal tool's only arguments are `pipeline` and
`claimed_deferred_intent_ids` (`:1474-1491`). Its "information gaps"
(`:190`, `:306`) are catalog-discovery needs, not questions to the user. A
case-insensitive search of `pipeline_planner.py` for a question or
clarification channel found none (the same search matched the information-gap
lines, so the instrument fires). Not checked: whether the `pipeline` schema
admits interpretation placeholders that would raise a review card.

So a freeform question or concern written as text on the first build turn is
dropped, and if the nudge budget is spent the user receives a planner failure
rather than the model's words.

Documented rationale: mid-plan prose is "thinking aloud", and marker-less text
is "ambiguous between narration and decline" (`:1586-1591`). A question to
the user is a third thing the discriminator does not represent.

### 3. Tool-call-turn prose is overwritten once advisor context is in play

`_persist_turn_audit` (`service.py:5371-5375`): when
`advisor_repair_context_introduced` is set, the assistant row for every
subsequent tool-call turn is persisted as "ELSPETH is applying a pipeline
correction." with `raw_assistant_content=None`. The flag is set by the END
gate's repair-continue (`:7080`) and by the early advisor checkpoint
(`:7209`), so it can flip mid-turn with no terminal block ever occurring.

One row is written, and it holds the substitute: `turn_audit.py:313-319`
states that "`assistant_message` is already the substituted message on the
advisor-repair branch". `assistant_row_uses_current_dispatch` is an
identity-proof bit for the turn-end de-duplication, not a second row.

The user-facing withheld-prose disclosure is deliberately left off this line
(`no_tool_policy.py:125-127`: "the transient repair status line ... is
deliberately excluded").

## Disclosed replacements (user is told, prose still not recoverable)

### 4. Advisor END-gate block

`_advisor_blocked_result` (`service.py:8293`) does `del assistant_message`,
sets `raw_content = ""`, and composes fixed copy over the empty string. The
prefix invariant at `:8456` is therefore vacuous on this branch. All five
reasons take it.

Its own docstring (`:8330-8360`) states what is withheld but gives no reason.
The only stated rationale in the tree is the repair cohort's
(`no_tool_policy.py:396-401`): prose written *after hidden advisor findings
entered the model's context* may quote or rebut them. Borrowing that
rationale, the withholding is justified only when a repair-continue injection
(`:6789`) preceded the block. `_advisor_blocked_result` does not receive that
fact: its inputs are `reason`, `verdict` and the preflight, and `reason` is
independent of it in both directions.

- Pass 1 FLAG, injection, model replies, pass 2 advisor outage: reason is
  `unavailable`, yet findings did enter context.
- Pass 1 advisor outage, pass 2 FLAG on the last pass: reason is
  `flagged_final_pass`, yet nothing was ever injected.
- `flagged_unrepairable` blocks on the first pass by design
  (`verdict.repair_unactionable`, `:6660`), so no injection can precede it.
- Any first-pass block with no early checkpoint: no injection.

In the uninjected cases the primary model's reply is deleted although nothing
hidden could have leaked into it, including on a pipeline that validated
green during an advisor outage.

Already tracked: elspeth-ff4f0068a4 added the user-facing sentence
(`ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE`), elspeth-2306940c70 added the
model-facing disclosure row. Both address the silence, not the removal.

### 5. Advisor repair terminal

`_replace_advisor_repair_public_result` (`service.py:1340`) is applied at both
loop exits (`:7065`, `:7401`) whenever `advisor_repair_context_introduced` is
set, **including when the next checkpoint returned CLEAN**. Seven branches,
all fixed copy, `raw_assistant_content` `None` or `""`. The success branch
publishes "The pipeline is configured and ready." plus the withheld
disclosure in place of the model's summary of what it changed.

### 6. Planner staging

`_stage_pipeline_plan` (`service.py:5039-5062`) publishes one of four
`PIPELINE_STAGED_*` constants. The inline comment asserts "this surface's
model emitted a tool call, never prose". That is unmeasured: a provider can
return `content` alongside `tool_calls`, and `_assistant_tool_calls_message`
(`pipeline_planner.py:1785`) keeps such content in planner-internal history
only.

The proposal card carries no model words either. Its `summary` and
`rationale` come from `build_tool_proposal_summary` (`proposals.py:37`), which
is server-authored: the rationale is the constant "Requested by the current
composer turn." and the summary is a node count. On the planner surface the
only model-authored text that can reach the user is a `DECLINE:` body.

### 7. Guided scaffold-leak guard

`_require_prose_assistant_message` (`guided/chat_solver.py:228`) rejects the
whole reply if its lowercased text contains `<tool_call`, `</tool_call`,
`<tool_response` or `</tool_response`. The user gets `_SCAFFOLD_LEAK_MESSAGE`
("didn't pass a quality check, so it wasn't shown"). Substring match over the
full reply, so a reply that legitimately quotes one of those tags is removed
too. Status is recorded as `SYNTHETIC_UNAVAILABLE`; only `error_class`
distinguishes it.

### 8. Guided shape and commit rejections

`_MODEL_SHAPE_REJECTED_MESSAGE` (`_guided_step_chat.py:703`, `:950`),
`_COMMIT_REJECTED_MESSAGE`, `_PAIRED_SOURCE/SINK_NOT_APPLIED_MESSAGE` (`:569`,
`:829`) and `_DEFERRED_MANAGEMENT_REPAIR_MESSAGE` replace the model's
`assistant_message` when the accompanying tool arguments are rejected. The
reply is coupled to the action: reject the action and the explanation goes
with it. These fire only at in-Send repair exhaustion.

## Checked and not a removal

- `composer_turn_end_assistant_row` (`routes/_helpers.py:1493`) persists only
  the backend suffix when the prose is already committed on the tool-call row.
  It is de-duplication and "fails toward persisting the full message".
- `MessageBubble.tsx` renders `content` for assistant rows that carry
  `tool_calls`; only the tool-call list is collapsed. The one segment filter
  (`:66-72`) hides a single stale trusted notice, not model prose.

## Not measured

- How often each case fires. The advisor cases leave
  `AdvisorTerminalPublication` audit rows and the planner leaves
  `prose_nudged` attempt rows, so cases 2, 4 and 5 are countable from a
  session database. Cases 1 and 3 leave no row that says prose was dropped.
- Whether real provider responses carry `content` beside the terminal
  proposal call (case 6).
- `guided/protocol.py:1258 _MAX_CURRENT_TURN_TEXT` and the
  `guided_replay.py:528` kind filter were not read.

## Observation

Cases 1 to 5 share one root: there is no place to keep a model reply that the
backend decides not to publish. The choice is publish or destroy, so every
gate that must not publish destroys. A persisted, non-rendered record of the
withheld text would make every row of the table recoverable without changing
what the user sees. That is separate from case 1's other half, the reply
missing from the model's own context, which is an `llm_messages` question:
`_composer_conversation_messages` excludes `audit` rows from prompt history by
construction, so an audit row would not restore it. Whether to keep, narrow or
remove each withholding is a separate decision for each site.
