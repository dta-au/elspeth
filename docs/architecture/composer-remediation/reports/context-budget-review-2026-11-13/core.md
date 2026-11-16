# Composer context budget: adversarial review of the core mechanism

Reviewer: ctx-review-core. Tree: `.claude/worktrees/composer-remediation`,
branch `integration/composer-remediation`. Started at `ca8b36001`; HEAD moved
to `677f9b922` during the review. The three core files and `web/config.py` are
unchanged between the two (empty `git diff --stat`), and the scratch copy is
byte-identical to the tree for all three at the end.

Scope: `src/elspeth/web/composer/context_budget.py`,
`context_budget_apply.py`, `context_budget_history.py`, and the four settings
in `src/elspeth/web/config.py`. Call sites were read only to learn inputs and
outputs. Nothing in the tree was edited; experiments and the mutation copy are
under the session scratchpad `ctx-core/`.

## Verdict

The pure decision (`decide_context_budget`) does what its docstring and the
design say: boundaries, order, stickiness, the floor and determinism all held
under 35,106 randomised trials and exact-boundary probes. The defects are in
what feeds it. The token estimate is not a safe over-estimate, and a step in
the bytes-per-token ratio defeats the hysteresis. Neither loops, sends an empty
request, or orphans a tool call.

## What the mechanism is (as built)

- Measure: canonical UTF-8 JSON bytes of `{messages, tools}` with cache
  markers, prompt-guard keys detached. No tokenizer.
  `tokens = ceil(wire_bytes / ratio)`.
- Ratio: `request_wire_bytes / prompt_tokens` of the session's newest
  successful budgeted call on the same model, else any model, clamped to
  2.0-6.0, else 3.0. It is read once per turn
  (`sessions/composer_turn.py:1504`), so every call of a turn uses the
  previous turn's ratio.
- Thresholds (verified, cap 100, ratio 1.0): shed starts at `tokens > high`
  (85 no shed, 86 sheds); shedding stops at `tokens <= low` and
  `bytes <= max`; a previously shed set is released at `unshed tokens <= low`
  (60 released, 61 held); refusal at `tokens > cap` (100 admitted, 101
  refused) or `bytes > max`.
- State: none in process. The sticky history set is the
  `shed_history_indices` of the newest stored call with a non-null
  `context_budget`; within a turn it is united with every earlier call of the
  turn, and tool stubs are held for the turn regardless of size. Restart,
  fork and concurrency therefore reduce to "what is stored".
- Order: tool results, assistant prose, hatch events, user requests; caller
  order within a kind. Sticky segments first.

## Defects

### D1. The estimate under-counts dense content; a request is admitted over the cap (medium, VERIFIED)

`context_budget.py:19-47` and `:193-194`; ratio source
`context_budget_history.py:156-159`.

The code comment says "the 3.0 fallback is below every honest wire ratio, so
... the estimate is high", and the design says the fallback "sheds early,
never late". Measured with `tiktoken` `o200k_base` on the canonical wire JSON
(`ctx-core/exp2.py`):

| Content | wire bytes per token |
|---|---|
| English prose | 5.25 |
| Chinese / Japanese prose | 3.91 |
| emoji | 3.00 |
| integer ids | 2.18 |
| sha256 hashes in JSON | 2.06 |
| JSON nested as a string | 1.99 |
| numeric CSV preview | 1.80 |
| UUIDs | 1.70 |
| base64 | 1.46 |

Hashes, UUIDs and numeric previews are what composer tool results carry. They
sit below the 3.0 fallback and at or below the 2.0 clamp floor. The ratio is
calibrated on the previous turn's request, which is prose-dominated, and is
frozen for the turn.

Trigger (`ctx-core/exp4.py`, real `request_wire_bytes`,
`tool_result_segments`, `decide_context_budget`): cap 100,000; previous turn
measured 4.78 on a 256 KB prose prefix; this call adds one 175 KB tool result
of row ids, hashes and amounts in the latest round trip. Decision: estimated
92,406 tokens, nothing sheddable, admitted. Tokenizer count of the same body:
144,146, 144% of the cap. On a new session at the 3.0 fallback: estimate
85,158 (85%, admitted untouched), tokenizer 124,781 (125%), true ratio 2.05.

Why it matters: the design goal is that no compose-loop or planner request is
rejected by the gateway for size. Within a tool-heavy turn the budget neither
sheds nor refuses, and the provider's `context_length_exceeded` returns. The
design does disclose "an estimate, not a tokenizer"; what is wrong is the
claimed direction of the error. Caveat: one tokenizer, not the deployment's
model; no live provider was run.

Smallest fix: anchor on the provider's own count instead of re-estimating the
whole request. The turn's earlier calls are already in hand
(`recorder.llm_calls` carry `prompt_tokens` and `request_wire_bytes`), so
estimate `last_prompt_tokens + ceil((wire_now - wire_then) / 2.0)`: the
measured part is exact and only the growth is priced pessimistically. Failing
that, correct the two "never late" claims.

### D2. A ratio step releases the whole shed set: shed, regrow, shed (medium, VERIFIED)

`context_budget.py:209` (`released = fits_low(request_bytes)` at the current
ratio) with `context_budget_history.py:99-102`.

Release is judged at today's ratio; the shed was decided at an earlier one. The
guaranteed step is fallback 3.0 to the first measured ratio. Release needs the
ratio to rise by `shed_above / shed_to` = 1.42x; 3.0 to 4.3 is 1.43x, to 4.6
is 1.53x.

Trigger (`ctx-core/exp1.py`): cap 100,000, 270 KB request with 120 KB of
history.
- Turn 1, ratio 3.0: estimate 90,000, 15 messages shed, down to 60,000.
- Turn 2, same bytes, ratio 4.6: estimate 58,696, shed 0. All 15 return.
- Turn 3, grown: estimate 86,957, 20 shed.

The window: a session whose first shedding turn ran on the fallback releases
on the next turn if its unshed estimate at 3.0 was under `cap * 0.6 * r / 3`:
86% of cap at r=4.3, 92% at 4.6, 100% at 5.0 and above. That is every
existing long session on its first turn after upgrade, the case the changelog
calls out. The user sees "earlier conversation is no longer visible" one turn
before it is visible again, the conversation tail cache breaks twice, and the
audit trail shows a shed with no matching shrink. Any other 1.42x step does
the same (a provider that starts or stops counting cached tokens moves an
honest 4.3 to the 6.0 clamp, 1.40x, just short).

Smallest fix: judge release at the smaller of the current ratio and the ratio
the stored shed was decided at. `bytes_per_token` is already on the stored
`context_budget` record; `ContextBudgetHistory` would carry it beside the shed
set.

### D3. When the fixed prefix is above the low mark, one crossing sheds every message, user requests included (medium, VERIFIED)

`context_budget.py:221-225`. The loop stops only at the low mark, and nothing
checks the low mark is reachable.

Trigger (`ctx-core/exp7.py`): cap 128,000, the brief's measured prefix of
274,506 bytes, 40 history messages totalling 56 KB, fallback ratio. Estimate
110,169 exceeds the high mark of 108,800. All 40 messages are shed, 20 of
them user requests; the result is 91,502, still above the low mark of 76,800.
Next turn at a measured 4.4 the estimate is 75,797, under the low mark, and
all 40 return (D2).

The low mark is below the fixed prefix whenever
`cap < prefix_bytes / (ratio * shed_to)`: 152,500 tokens at the fallback and
106,400 at 4.3 for that prefix. A 128k-window deployment is inside the first
range. There the order of sacrifice is moot: 2% over the high mark costs the
whole conversation, where shedding about 1.5 KB would have cleared it.

Smallest fix: when the protected size alone is above the low mark, stop at the
high mark instead (or at `protected + shed_to * (high - protected)`), and
record that the low mark was unreachable. This one is a design decision for
the maintainer, not a one-line change.

### D4. A budgeted call that labels no history erases the stored sticky set (low, VERIFIED in the history module, READ at the call site)

`context_budget_history.py:95-96` takes the set from the newest budgeted call
whatever it labelled. `pipeline_planner.py:4615-4622` drops the conversation
context on a hatch turn when the transcript covers the prior requests and
there is no assistant prose; that call has no history segments, so it records
`shed_history_indices=[]`.

Trigger (`ctx-core/exp4.py` E11): stored set `{0,1,2,3}`; one later call
recording `[]`; next turn with the unshed request at 80% of cap sheds nothing,
where the stored set would have kept four messages shed. The route is narrow
(needs a hatch turn on a session with no assistant prose).

Smallest fix: have each record carry forward the previously shed history keys
it did not label, or take the set from the newest call that labelled history.

### D5. A reused tool-call id stubs results that were never under pressure (low, VERIFIED)

`context_budget_apply.py:162-164` keys by `tool:<tool_call_id>` and
`:180-187` holds by the same key.

Trigger (`ctx-core/exp6.py`): three rounds all using id `call_0`; round 0 was
stubbed earlier in the turn. With the request at 493 estimated tokens against
a cap of 1,000,000, the decision sheds `tool:call_0` and the positions list
stubs rounds 0 and 1. Round 1 was never chosen. Only matters for a provider
that repeats ids across rounds; the docstring says that can happen.

Smallest fix: key by id plus occurrence ordinal.

### D6. The decision does not enforce its own input contract (low, VERIFIED)

`context_budget.py:211, 227`. A `saving_bytes` larger than the request gives a
negative `request_bytes_after` and `estimated_tokens_after` and is admitted
(90-byte request, one 500-byte segment: -410). Duplicate keys are summed
twice. No call site does either today. Smallest fix: raise `ValueError` when
the savings exceed `request_bytes` or a key repeats.

## Config

- Required-ness and types hold (VERIFIED): the cap is required, strict int;
  `True`, `1.5`, `"2e5"` and NaN marks are rejected at startup; an integer `1`
  for `shed_above` is coerced to `1.0`, so the policy's exact-float check
  cannot fire; the env loader accepts `1`, `.5` and `200_000`.
- **C1 (low, VERIFIED). A cap the fixed prefix cannot fit starts cleanly and
  refuses every first request.** `config.py:375` has `ge=1` and nothing else;
  `composer_context_max_prompt_tokens=1` and
  `composer_planner_max_request_bytes=1` both start. With the measured prefix,
  any cap under 91,502 refuses the first call of every new session at the
  fallback ratio, although the provider counted 49,776 tokens for that request
  (`exp1.py` E3: 91,000 refused, 92,000 admitted). A refusal leaves no call
  record, so no ratio is ever measured and the session cannot leave the
  fallback. Smallest fix: document the floor (cap must exceed prefix bytes /
  3); better, a startup or doctor check against the rendered prefix.
- No relation is checked between the cap and
  `composer_planner_max_completion_tokens` or a model window. The brief asked
  for "window minus completion allowance minus margin"; that subtraction is
  left to the operator and the upgrade note does not say so.
- The marks are float products compared with integer token counts:
  `0.57 * 100` is `56.99999999999999`, so 57 tokens does not meet a 57% mark.
  One token, harmless.

## Design questions

1. The ratio is frozen for a turn and comes from the previous turn's last
   call. Within a turn the provider's count for the call just made is on the
   recorder and unused. Intended?
2. Refusal is absorbing: no record, no new ratio, and the session only grows.
   The recourse text says start a new session, so this may be intended, but a
   fallback-ratio refusal (C1) on a request the provider would accept is the
   same absorbing state.
3. The clamp floor of 2.0 is above measured ratios for dense content, so the
   clamp can push an honest measurement in the unsafe direction.

## Tests

Baseline: `test_context_budget.py` and `test_context_budget_history.py`, 72
passed, exit 0. The three call-site files (compose loop, planner, reply
stage), 113 passed, exit 0, run from the scratch copy as the harness control.

44 mutants, one at a time in the scratch copy; 33 killed, 11 survived. Core
and history mutants ran against the two unit files; survivors and all
`context_budget_apply.py` mutants were re-run against all five files.

Survivors:

| Mutant | Line | Reading |
|---|---|---|
| M01 high mark `>` to `>=` | `context_budget.py:212` | boundary not pinned |
| M03 token refusal `>` to `>=` | `:229` | boundary not pinned |
| M04 byte refusal `>` to `>=` | `:231` | boundary not pinned |
| M05 byte ceiling in `over`, `>` to `>=` | `:212` | boundary not pinned |
| M07 byte clause of `fits_low`, `<=` to `<` | `:197` | boundary not pinned |
| M20 `remaining` no longer excludes held keys | `:217` | held is a subset of previously shed at every call site; unpinned in the pure tests |
| M23 `escaped_bytes` counts characters, not UTF-8 bytes | `:58` | no test sizes non-ASCII history; the error is in the safe direction |
| A05 `saving <= 0` to `< 0` | `context_budget_apply.py:160` | a result exactly the stub's size is untested (would raise in `SheddableSegment`) |
| A09 reused id keeps only the first position | `:163` | reused ids untested (see D5) |
| H08 envelopes in a row read oldest first | `context_budget_history.py:85` | production writes one envelope per row; ordering is unexercised |
| H09 non-audit rows not skipped | `:83` | unpinned guard |

Every equality boundary of the three ceilings survives: only the low mark's
`<=` (M02) is pinned. Killed, and so pinned: shed order reversed or unsorted,
stopping at the high mark, either floor check skipped, release judged on the
kept size, never releasing, held not sticky, `over` judged on the unshed
size, ceil to floor, ratio ignored, both clamp bounds, failed call supplying
the ratio, oldest or union shed set, per-model ratio, stub dropping
`tool_call_id` or keeping prompt-guard keys, first instead of last round
trip, saving ignoring the stub, in-turn stickiness, tools left out of the
wire size, both allowances zeroed, the announce-once rule.

Gaps beyond the survivors:
- No test file for `context_budget_apply.py`; its helpers are covered only
  through the call-site tests.
- No property test on `decide_context_budget`. The invariants are cheap:
  admitted implies both ceilings hold, refusal implies nothing sheddable is
  left, feeding a decision's shed set back gives the same set, same input
  gives the same output.
- No test moves the ratio between two decisions (D2), puts the protected size
  above the low mark across turns (D3), or uses non-ASCII or dense content
  anywhere (D1, M23).

## What I tried that held

- 35,106 random policies, segment sets, previously-shed and held sets
  (`exp5.py`): 0 ceiling violations among admitted decisions, 0 refusals with
  anything left to shed, 0 fixed-point failures, 0 kind-order violations, 0
  non-determinism, 0 input mutation, held keys always shed.
- Exact boundaries at each threshold match the design text.
- At a fixed ratio the shed set cannot oscillate: release needs the unshed
  request at or below the low mark, and that implies it is not over.
- Pairing: a stub keeps `role` and `tool_call_id`; history is removed as
  whole messages; the system prompt, catalog, state message, current user
  message and latest round trip are never labelled as segments by either
  call site.
- Both paths index history into the same `chat_history` list, so a set shed
  on the compose loop is honoured on the planner and the reverse.
- Refusal: reached only with everything shed, names tokens before bytes, and
  reports the fully shed size. No path loops or sends an empty request.
- Purity: no clock, no process state, no dict-order dependence; the policy
  hash changes with each parameter.

## Not done

No live provider and no provider tokenizer other than `o200k_base`. No
PostgreSQL, full suite or whole-tree gate. Role alternation per dialect after
history removal, fork and revert effects on stored indices, and the audit
record were left to the other two reviewers.

```json
{"findings": [
  {"title": "Token estimate under-counts dense tool results; a request is admitted at 144% of the cap",
   "severity": "medium", "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget.py", "src/elspeth/web/composer/context_budget_history.py"],
   "repro": "PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src .venv/bin/python <scratchpad>/ctx-core/exp4.py (E10, E10b) and exp2.py",
   "detail": "Ratio is calibrated on the previous turn's prose-heavy request and frozen for the turn. Hashes, UUIDs and numeric previews tokenise at 1.5-2.2 wire bytes per token (o200k_base), below the 3.0 fallback claimed to be 'below every honest wire ratio' and at or below the 2.0 clamp floor. Estimate 92,406 admitted; tokenizer 144,146 against a cap of 100,000. One tokenizer, no live provider."},
  {"title": "A step in the bytes-per-token ratio releases the whole shed set (shed, regrow, shed)",
   "severity": "medium", "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget.py", "src/elspeth/web/composer/context_budget_history.py"],
   "repro": "<scratchpad>/ctx-core/exp1.py E1: turn 1 ratio 3.0 sheds 15; turn 2 same bytes ratio 4.6 sheds 0; turn 3 sheds 20",
   "detail": "Release is judged at the current ratio (context_budget.py:209). Fallback 3.0 to a measured 4.3-4.6 is a 1.43-1.53x step, above shed_above/shed_to = 1.42, so a session that first sheds on the fallback re-admits everything next turn. Affects existing long sessions on their first turn after upgrade."},
  {"title": "Fixed prefix above the low mark: one crossing of the high mark sheds every history message including all user requests",
   "severity": "medium", "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget.py"],
   "repro": "<scratchpad>/ctx-core/exp7.py: cap 128000, prefix 274506 bytes, 40 messages; 40/40 shed, result still above the low mark",
   "detail": "The shed loop stops only at the low mark and nothing checks the mark is reachable. True for caps under 152,500 at the fallback ratio and under 106,400 at 4.3 with the measured prefix."},
  {"title": "A budgeted call that labels no history erases the stored sticky set",
   "severity": "low", "confidence": "probable",
   "files": ["src/elspeth/web/composer/context_budget_history.py", "src/elspeth/web/composer/pipeline_planner.py"],
   "repro": "<scratchpad>/ctx-core/exp4.py E11 (history module executed; hatch route at pipeline_planner.py:4615-4622 read, not driven end to end)",
   "detail": "from_records takes shed_history_indices from the newest budgeted call whatever it labelled; a hatch call without a conversation context records an empty list."},
  {"title": "Reused tool-call id force-stubs results that were never under budget pressure",
   "severity": "low", "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget_apply.py"],
   "repro": "<scratchpad>/ctx-core/exp6.py: three rounds with id call_0, held tool:call_0, estimate 493 of cap 1,000,000, positions [3, 5] stubbed",
   "detail": "Segments and held keys are keyed by tool_call_id alone, so a later result sharing an id is stubbed as soon as it leaves the latest round trip."},
  {"title": "A token cap below the fixed prefix's fallback estimate starts cleanly and refuses every first request",
   "severity": "low", "confidence": "confirmed",
   "files": ["src/elspeth/web/config.py", "src/elspeth/web/composer/context_budget.py"],
   "repro": "<scratchpad>/ctx-core/exp1.py E3 and exp3b.py: cap 91000 refused on a 274,506-byte prefix the provider counted as 49,776 tokens; cap=1 and max_request_bytes=1 pass WebSettings",
   "detail": "config.py:375 has ge=1 only. A refusal leaves no call record, so no ratio is measured and the session stays on the fallback."},
  {"title": "Every equality boundary of the three ceilings is unpinned; 11 of 44 mutants survive",
   "severity": "low", "confidence": "confirmed",
   "files": ["tests/unit/web/composer/test_context_budget.py", "tests/unit/web/composer/test_context_budget_history.py"],
   "repro": ".venv/bin/python <scratchpad>/ctx-core/mutate.py <tree> <copy> M01 M03 M04 M05 M07 M20 M23 A05 A09 H08 H09 (results in mut-core.out and mut-wide.out)",
   "detail": "Survivors: > to >= at the high mark, token cap and byte cap (three sites), held-key exclusion, escaped_bytes counting characters, saving == 0, reused-id positions, envelope order, non-audit row guard. No test file for context_budget_apply.py and no property test on the decision."}
]}
```
