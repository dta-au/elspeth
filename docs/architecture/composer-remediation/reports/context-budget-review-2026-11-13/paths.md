# Composer context budget (0.8.13): call-site review

Reviewer: ctx-review-paths. Tree: `.claude/worktrees/composer-remediation`, branch
`integration/composer-remediation`. Subject: `git log 43e5f66cb..be60978f9` (55 commits).
The worktree was not edited (`git status --short` is empty; this file is under the ignored
`.lanes/`). Every experiment ran in scratch copies under
`<scratchpad>/ctx-paths/`:
`copy/` for mutants, `copy2/` for probes, logs beside them.

Labels: VERIFIED = I executed it and it showed the behaviour. READ = from the code only.

## Verdict

The budget is applied where the design says it is, to the bytes that are sent, on every round
and retry, and shedding is a view only. I found no unbudgeted compose-loop or planner dispatch,
no invariant violation and no tutorial branch. I found one behavioural defect in what is shed
(medium), two smaller defects in shed-set state and order (low), and three mutants that the
named test files do not kill.

## Real defects

### F1 (medium, VERIFIED): the feedback a repair must act on is stubbed first, and the stub says "call the tool again"

Where: `src/elspeth/web/composer/context_budget_apply.py:128` (`tool_result_segments` labels
every `role == "tool"` message), called from `pipeline_planner.py:3503` and `service.py:1702`.
The stub text is `context_budget.py:48`.

Every tool result outside the latest round trip is sheddable, whatever produced it, at the
first rank of the order of sacrifice and oldest first. The design's reason for that rank is
that tool results "can be fetched again by calling the tool". That holds for discovery
results. It does not hold for:

- the planner's rejection feedback (`_retain_terminal_rejection`, `pipeline_planner.py:4635`),
  the only place the planner is told why its candidate was refused;
- a failed mutation's error in the compose loop;
- the advisor tool's feedback (`tool_batch.py:2948`).

Because the order is oldest first, these small results go before the large discovery results
that follow them. The repair call then sees its own rejected candidate (arguments are never
shed) beside "[Result omitted by ELSPETH to fit the context budget. Call the tool again if you
need it.]". Emitting the same candidate again is a repeated rejection and spends the repair
budget.

Planner repro (VERIFIED, real `plan_pipeline` against the scripted provider):
`copy/tests/unit/web/composer/test_probe_paths_planner.py`, run before any mutant. Script:
emit an invalid pipeline (rejected with `edge_unknown_node`), `get_plugin_schema llm`,
`get_plugin_schema csv`, emit a valid pipeline; high mark between the third and fourth request.

    STUBBED IDS on the repair call: ('emit-bad', 'schema-llm')
    emit-bad result as sent: [Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]

`edge_unknown_node` appears nowhere in the fourth request. Log: `probeA.log`.

Compose-loop repro (VERIFIED, real loop): `copy2/tests/unit/web/composer/test_probe_paths_loop.py`.
Four tool rounds, the first a refused mutation whose result carries its reason. On the fifth
call `stubbed ids: ('call-1', 'call-2', 'call-3')` and the reason is absent from the request.
The refused result was about 500 bytes: it was shed for its position, not its size.
Log: `probeB.log`.

Trigger in production: a turn that crosses the high mark after a rejection or failed mutation
has been followed by at least one more tool round. A long session sheds to the low mark and
grows back toward the high mark, so sitting near it is the normal state of the sessions this
feature exists for.

Smallest fix: label only re-fetchable results. The producing call's name is on the assistant
message before each result, and `tools/discovery.py:81` (`is_discovery_tool`) already
classifies it. Give `tool_result_segments` a predicate and skip results whose call is not a
discovery call (planner: skip results of the terminal proposal tool). If those results must
stay sheddable as a last resort, give them their own kind ranked after `USER_REQUEST` and a
stub that does not say "call the tool again".

### F2 (low, VERIFIED): the reply stage releases history the loop is holding shed, and erases the shed set the next turn starts from

Where: `service.py:2363` (the reply stage's `_prepare_primary_delivery`), `context_budget.py:209`
(`released = fits_low(request_bytes)`), `context_budget_history.py:96`
(`shed = call.shed_history` from the newest budgeted call).

The release rule is judged on the request being fitted. The reply-stage request carries only
the recovery tools: 1,348 bytes of tool definitions against 77,015 for the loop
(`openai_strict`; 1,320 against 73,997 for `none`; measured). History the loop's calls keep
shed between the marks therefore fits under the low mark in the reply-stage request, is
released there, and that call's record carries an empty `shed_history_indices`. It is the
newest budgeted call of the turn, so the next turn's `ContextBudgetHistory` has no previously
shed history.

Repro (VERIFIED): `copy2/tests/unit/web/composer/test_probe_paths_reply.py`. Same service,
same history, same `previously_shed_history = {history:1}`:

    low/high marks (tokens): 93735.6 132792.1
    LOOP  call: tokens_before 102576 shed_history (1,)
    REPLY call: tokens_before 77705 shed_history ()

`copy2/.../test_probe_paths_history.py`: stored calls `[shed 1], [shed 1], [shed nothing]` give
`previously_shed_history == set()`. The existing
`test_history_shed_on_the_previous_turn_stays_shed_between_the_marks[frozenset()]` shows the
consequence on the next turn: nothing is shed between the marks.

Effect: after a turn that ends in a reply stage, history shed earlier returns on the next
turn while the request is still above the low mark, then is shed again at the high mark. That
contradicts the changelog ("shed history is restored only when the whole request is back at
or under SHED_TO") and changes the sent prefix twice. No ceiling is exceeded and nothing is
lost.

The same reset happens (READ) after an escape-hatch call whose conversation context was
folded into the transcript (`pipeline_planner.py:4616-4622`): that request has no history
segments, so its record's `shed_history_indices` is empty whatever was shed before.

Smallest fix: at the reply stage, pass the turn's already-shed history keys as `held`
(`service.py:1731`) so they cannot be released there; on the hatch call, carry forward the
previously shed history keys it has no segment for into the record.

### F3 (low; shedding order VERIFIED at the fitting function, trigger READ): on a hatch turn the oldest user request can be the first thing shed

Where: `advisor_transcript.py:636` (`sheddable_event_indices`: every event but the current
request's reference), `pipeline_planner.py:4616-4622` (the conversation context is dropped
when the transcript covers the prior requests and there is no prior assistant prose).

In that case the transcript holds the only copy of the earlier user requests. They are
`HATCH_EVENT` segments, shed oldest first, so they go before the current turn's tool events,
with no count, only the `oldest_events_omitted_for_context_budget` gap. The design's own
reason for shedding user requests last ("an old restriction may govern a new instruction")
applies to them.

Repro (VERIFIED, `_fit_planner_context_budget` with `request_conversation_context=None`):
`copy2/tests/unit/web/composer/test_probe_paths_hatch.py`. Events: a one-sentence human
restriction (142 wire bytes), a 20,000-byte tool event, the request reference; the request is
just over the high mark. Result: `shed hatch events: (0,)`. The restriction is gone and the
20 KB tool event is sent.

The design documents the order (Ruling 7), so this is as much a design question as a defect.
Smallest change if unwanted: have `sheddable_event_indices` return human-authored events
last, or keep the conversation context on the hatch request whenever events may be shed.

## Test gaps

Baseline: the six named files, `-n 2`: `200 passed`, exit 0 (`baseline.log`).

`test_advisor_mandatory_evidence_budget.py`, `test_provider_context.py` and
`test_provider_context_planner_integration.py` do not test this feature. They cover the
advisor's own hint allowance and provider-context capture; the range touched them only to
add the new required setting (and one refusal-code rename). `advisor_context.py`,
`provider_context.py` and `advisor_checkpoint.py` contain no context-budget code, so "drop
the mandatory-evidence protection" has no mutant in this change.

The compose-loop and planner tests are good evidence: they drive the real loop or the real
`plan_pipeline` against a recording `litellm.acompletion` and assert on the request the
provider received and on the call record's hashes. The reply-stage file drives a real turn
but two of its tests replace `_shed_to_context_budget`, the unit under test, to hand-feed a
refusal or a record; one test (`..._names_the_largest_component...`) uses the real decision.

18 mutants, each applied to `copy/` and run against the loop and reply-stage files (service
mutants) or the planner file (planner mutants). 15 killed, 3 survived.

| Mutant | Result | Killed by (first) |
|---|---|---|
| M1 loop: no shedding once the turn has a call | killed | `test_three_round_tool_turn_stubs_earlier_results...` |
| M2 reply stage: decision measures the loop form, before quoting and the closing instruction | **SURVIVED** | none |
| M3 loop: final byte guard removed | killed | `test_the_final_byte_guard_refuses_a_request_the_decision_did_not_fit` |
| M4 planner: hatch turn exempt from the ceilings | killed | `test_a_dispatched_hatch_request_never_exceeds_either_ceiling` |
| M5 loop: `held` dropped | killed | `test_a_result_stubbed_on_an_earlier_call_stays_stubbed_in_the_reply_stage_request` (only) |
| M6 planner: `previously_shed` forgotten | killed | `test_history_shed_on_an_earlier_turn_stays_shed_between_the_marks` |
| M7 loop: the last-chance call after the composition cap sends the unshed `llm_messages` | **SURVIVED** | none |
| M8 reply stage: refusal fails the turn | killed | `test_a_context_budget_refusal_at_the_reply_stage_leaves_the_finished_turn_standing` |
| M9 loop: latest round not protected | killed | `test_latest_round_with_parallel_tool_calls_keeps_both_results` |
| M10 loop: no omission allowance | killed | `test_a_request_within_the_re_render_margin_sheds_one_more_segment_and_is_sent` |
| M11 planner: latest round not protected | killed | `test_a_later_call_whose_protected_content_cannot_fit_is_refused` (only) |
| M12 loop: decision ignores cache markers | killed | `test_the_token_estimate_and_the_recorded_size_are_the_wire_bytes_of_the_request_sent[anthropic-marked]` |
| M13 reply stage: deadline check removed | killed | `test_a_reply_stage_that_is_out_of_time_prepares_no_request_and_announces_nothing` |
| M14 reply stage: sends the unfitted request | killed | `test_the_reply_stage_request_stubs_earlier_tool_results_and_keeps_the_latest_round` |
| M15 planner: refusal swallowed, request sent | killed | `test_an_oversized_intent_is_refused_before_any_provider_call` |
| M16 loop: user requests labelled protected, never shed | **SURVIVED** | none |
| M17 planner: hatch-turn context refusal masked by the hatch's exhaustion | killed | `test_a_hatch_request_whose_protected_content_cannot_fit_is_refused_after_shedding_every_event` |
| M18 loop: cross-turn shed set ignored | killed | `test_history_shed_on_the_previous_turn_stays_shed_between_the_marks` |

Survivors, and the test each needs:

- **M2** (`service.py:1717`). Nothing asserts that a reply-stage request near the token ceiling
  is within it as sent. Under this mutant the decision under-measures by the quoting overhead
  and the closing instruction; the final guard re-checks bytes only (`service.py:1624`), so a
  reply over the token ceiling would be sent. Add a reply-stage twin of
  `test_a_dispatched_request_never_exceeds_either_ceiling` on the token ceiling, asserting
  `ceil(wire_bytes(sent) / ratio) <= max_prompt_tokens`.
- **M7** (`service.py:2599-2626`). The third budgeted call site, the one last call after the
  composition-turn cap, has no context-budget test in these files. Add one test with
  `composer_max_composition_turns=1` and a history over the high mark, asserting the request
  the provider received is shed and matches its record.
- **M16** (`service.py:3700`). No compose-loop test sheds a user request or sees
  `prior_user_requests_omitted > 0`; only the planner has one. Add the loop twin of
  `test_oldest_user_requests_are_shed_last_and_counted`.

Also run and passing: `tests/unit/web/sessions/test_reply_stage_refusal_route.py` (1 passed),
and `tests/integration/web/composer/test_freeform_planner_failure_translation.py` with the two
route tests `test_compose_loop_context_budget_refusal_names_its_cause_on_the_operation_record`
and `test_send_message_passes_context_budget_history_from_the_transcript` (260 passed, exit 0).
I ran no wider suite and make no claim about one.

## What I checked and found sound

### 1. Coverage: every provider dispatch

Physical dispatch sites (`grep` for `acompletion(`): `provider_gateway._call_llm`,
`_call_text_llm`, `pipeline_planner.call_model_body`, `advisor_checkpoint.py:525`,
`sessions/_auto_title.py:477`.

| Path | Budgeted | Notes |
|---|---|---|
| Compose loop round (`service.py:1851`) | yes | every iteration goes through `_call_model_turn` |
| Last-chance call after the composition cap (`service.py:2599`) | yes | READ; no test (M7) |
| Reply stage (`service.py:2363`) | yes | the decision measures the quoted form plus the closing instruction and the recovery tools |
| Provider retry in `_call_llm_before_deadline` | yes | re-sends the same fitted messages and record; nothing is rebuilt |
| Planner call, repair turns, prose nudges, truncation notices | yes | all through `call_model_body` (`pipeline_planner.py:3980`), refitted each call |
| Escape hatch | yes | same function, the hatch model's ratio |
| `compose_recovery` (`service.py:1184`) | no | three fixed messages plus the user message; the route caps a message at 65,536 characters (`sessions/schemas.py:167`) |
| Advisor END gate, early checkpoint, advisor tool | no | documented, dta-au/elspeth#364 |
| Auto-title | no | excerpt-truncated |
| Run diagnostics (`_call_text_llm`) | no | documented |
| Boot probe | no | documented |

Final bytes (READ): after fitting, the gateway only applies the same cache-marker function
the decision measured with and detaches prompt-guard keys
(`provider_gateway.py:763-768`, `482-484`). `_litellm_acompletion` appends nothing. The
planner hashes, measures and sends one snapshot (`pipeline_planner.py:4001-4036`). The
delivery-confirmation spy in the loop tests proves hashed == recorded == sent.

One API note (design, not a defect today): `context_budget` defaults to `None` on
`_call_llm_with_audit` (`provider_gateway.py:1142`), so a future compose-loop caller that
forgets it sends an unbudgeted request and records "not a budgeted call". Making the
parameter required, with an explicit sentinel for `compose_recovery`, would make that a type
error.

### 2. What shedding removes

Protected by construction (READ): system, catalog, tools, state, the current user message,
recovery and retained-operation messages, the latest tool round, and every within-turn
non-tool message. The last group covers the preflight and proof repair messages
(`composition_completion.py:1134`, `1479`, `1656`), the END-gate and early advisor findings
(`advisor_checkpoint.py:1570`, `composition_completion.py:2582`), the anti-anchor hint
(`service.py:2228`), and the planner's prose-nudge, truncation and hatch notices. All are
user-role messages, and only tool-role results are labelled.

Not protected: see F1.

Nothing downstream indexes the shed view (READ). Both call sites shed a copy; `llm_messages`
and the planner's `messages` stay unshed, so the elision indices (`service.py:3085`, `3360`),
`_reply_only_protocol_echo` and the transcript owner are unaffected. A stubbed result does
not count as a delivered inspection: `prepare_delivery` matches on the content hash
(`turn_effects.py:519-527`), so coverage falls honestly.

Pending interpretation reviews live in the state block and the database, not in history.

### 3. Persistence versus view

A view only (READ, and asserted by `assert history == _long_history(base)` and
`assert llm_messages == before`). Each turn rebuilds `chat_messages` from the stored records
(`composer_turn.py:1040`), never from a shed view. The `history:<index>` keys index that
rebuilt list. I looked for anything that could shift the indices between turns and found
none: no code deletes chat rows; the current user row is excluded only at the tail; private
evidence rows are skipped on both read paths; a fork copies a prefix. The compose loop and
the planner index the same list (`prompts.py` `history_message_indices`,
`planning_application.py:214`). The route passes `ContextBudgetHistory.from_records(records)`
and the records include `llm_call_audit` rows (`transcript_reads.py:25` filters diagnostic
evidence only); the wiring test passes.

### 4. Refusal paths

- Compose loop: a `ComposerConvergenceError` with `reason="context_budget_exceeded"`, raised
  before dispatch, handled like every other convergence failure (422, partial state kept).
  Nothing inside the loop catches and retries it; the only catch is the reply stage's, which
  re-raises any other reason (`service.py:2379`).
- Planner: `CONTEXT_BUDGET_EXHAUSTED` is raised without engaging the hatch and surfaces as
  itself on a hatch turn (M17 killed).
- Retry cannot loop on provider spend: the user row is already stored, a retry refits
  deterministically and refuses before any call. History is fully sheddable and a message is
  capped at 64 KiB of characters, so a session is permanently refused only when the state
  block, the fixed prefix or a latest tool round alone exceeds a ceiling.
- Reply stage: over budget completes the turn with the fixed notice and an audit row (M8
  killed). Out of time prepares nothing (M13 killed). Budget and time both exhausted (READ):
  the time check comes first (`service.py:2350`), so the user sees "reply is unavailable.
  Please retry", and the retry would then meet the budget refusal. Exactly at the ceiling is
  the core's `>` comparison; I did not test it here.

### 5. Invariants

No server-authored structure or conversation (READ of the whole range diff for the scoped
files). The server writes: the constant stub, two integer counts in the state block or
conversation context, the constant transcript gap marker, and two fixed notices marked as
trusted system segments. A refused reply stage yields an empty assistant message plus the
notice, the same shape as a provider failure. The range adds no line mentioning `tutorial`
or `provider="server"` in `web/composer` or `web/sessions`, and the only production caller of
`compose` is `composer_turn.py:1488`.

### 6. Concurrency and cancellation

There is no in-memory budget state (READ): `_ContextBudgetFrame` is per turn, and the shed
set and ratio are derived from stored records and the turn's own recorder. Turns on one
session are serialised by the COMPOSE lease, and the history is read under it. A provider
retry reuses the fitted request; the failed attempt's record carries the same shed keys, so
`first_history_shed_of_turn` announces once. A cancelled turn can at worst leave an older
record as the newest, which repeats an earlier decision.

## Design questions

1. The stub text is wrong at the reply stage, where no authoring tool is advertised: "Call
   the tool again" invites a call the gateway rejects as malformed
   (`provider_gateway.py:831-839`), which ends in "reply unavailable". Speculative on model
   behaviour.
2. Protected history rows grow without bound: replayed control rows (anti-anchor hints,
   advisor-withheld disclosures) and `route_system_message` rows have kind `None`
   (`service.py:3693-3703`). Each is small. They also outlive the user requests they refer to.
3. A session large enough to shed is the session whose advisor request (unbudgeted, whole
   transcript) is most likely to be rejected. Documented as #364; worth confirming what the
   END gate does to the user's reply when the advisor is unavailable every turn.
4. On a hatch turn the final guard `REQUEST_BYTES_EXHAUSTED` still surfaces as the hatch's
   original exhaustion (`pipeline_planner.py:4718`), unlike `CONTEXT_BUDGET_EXHAUSTED`. The
   guard is unreachable while both policies read one setting.

## Not done

No PostgreSQL, live provider or browser run. No full suite. I treated `context_budget*.py`
arithmetic as a black box except where a call site's use of it produced F2.

```json
{"findings": [
  {"title": "Non-refetchable tool results (planner rejection feedback, failed-mutation errors, advisor feedback) are stubbed first, with a stub telling the model to call the tool again",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget_apply.py", "src/elspeth/web/composer/pipeline_planner.py", "src/elspeth/web/composer/service.py", "src/elspeth/web/composer/context_budget.py"],
   "repro": "Scratch copy: pytest tests/unit/web/composer/test_probe_paths_planner.py -n 0 -s (copy/) and tests/unit/web/composer/test_probe_paths_loop.py -n 0 -s (copy2/). Planner script: emit invalid pipeline, get_plugin_schema llm, get_plugin_schema csv, emit valid; high mark between requests 3 and 4. Fourth request carries the stub for the rejection feedback (stubbed ids ('emit-bad','schema-llm')) and no 'edge_unknown_node'.",
   "detail": "tool_result_segments labels every role=tool message outside the latest round as sheddable at the first rank, oldest first. Rejection feedback and failed-mutation errors cannot be re-fetched, are small, and are older than the discovery results that follow them, so they go first. The repair call sees its rejected candidate beside a stub saying 'Call the tool again', which for emit_pipeline_proposal is a repeated rejection that spends the repair budget. Fix: label only results of discovery calls (is_discovery_tool), or rank the others after USER_REQUEST with a different stub."},
  {"title": "Reply stage releases history the loop holds shed and resets the next turn's shed set",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/service.py", "src/elspeth/web/composer/context_budget_history.py"],
   "repro": "Scratch copy2: pytest tests/unit/web/composer/test_probe_paths_reply.py tests/unit/web/composer/test_probe_paths_history.py -n 0 -s. Same history and previously_shed {history:1}: loop call tokens_before 102576 sheds (1,); reply-stage call tokens_before 77705 sheds (); from_records over [shed 1],[shed 1],[shed nothing] returns an empty previously_shed_history.",
   "detail": "The release rule is judged on the request being fitted, and the reply-stage request carries about 75 KB fewer tool definitions (1,348 vs 77,015 bytes). Sticky history is released there and the reply call, the newest budgeted call of the turn, records an empty shed set, so the next turn restores history while still above the low mark. Contradicts the changelog's release rule and churns the sent prefix; no ceiling is exceeded. The same reset follows a hatch call whose conversation context was folded into the transcript (read, not executed). Fix: pass the turn's shed history keys as held at the reply stage."},
  {"title": "On a hatch turn with the conversation context folded into the transcript, the oldest user request is shed before newer, larger tool events",
   "severity": "low",
   "confidence": "probable",
   "files": ["src/elspeth/web/composer/advisor_transcript.py", "src/elspeth/web/composer/pipeline_planner.py"],
   "repro": "Scratch copy2: pytest tests/unit/web/composer/test_probe_paths_hatch.py -n 0 -s. _fit_planner_context_budget with request_conversation_context=None and events [human restriction, 20 KB tool event, request reference], just over the high mark: shed_hatch_event_indices == (0,), the restriction is absent and the tool event is sent.",
   "detail": "sheddable_event_indices returns every event but the current request's reference and events are shed oldest first regardless of authorship. When _engage_escape_hatch drops the conversation context (transcript covers prior requests, no prior assistant prose) the transcript holds the only copy of earlier user requests, so they go first, with no count. The shedding order was executed; the production trigger was read. Documented order (Ruling 7), in tension with the design's reason for shedding user requests last."},
  {"title": "Three call-site mutants survive the named context-budget tests",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["tests/unit/web/composer/test_compose_loop_context_budget.py", "tests/unit/web/composer/test_reply_stage_context_budget.py", "src/elspeth/web/composer/service.py"],
   "repro": "Scratch copy: python mutants.py (18 mutants; results in mutants.out / mutants.json). Survivors: M2 (service.py:1717 reply-stage decision measures the loop form before quoting and the closing instruction), M7 (service.py:2612 last-chance call sends unshed llm_messages), M16 (service.py:3700 user requests labelled protected in the loop). Each run: 56 passed, exit 0.",
   "detail": "No test asserts a reply-stage request is within the token ceiling as sent (the final guard re-checks bytes only); the last-chance call after the composition-turn cap has no budget test; no compose-loop test sheds a user request or sees prior_user_requests_omitted > 0. 15 other mutants were killed."}
]}
```
