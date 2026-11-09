# Composer context budget — Phase 1 evidence

Brief: [composer-context-budget-brief.md](../composer-context-budget-brief.md).
Base: `eac8cfd4` (tip of `claude/vibrant-euler-tora25`, fix 4A byte split
recorded on every LLM call row).

Status: **code-side growth-path review complete; records-side classification
pending the affected user's session ids.** No limit is named yet. This file
names none by inference.

## 1. Records-side classification (steps 1–4)

**PENDING.** This session has no access to the deployment's sessions store.
The queries are written below so they can be run against it as-is; their raw
output goes in this section, one block per query, before any conclusion is
drawn.

```sql
-- Q1: every composer LLM call in a session, in order (one row per envelope)
SELECT m.sequence_no, m.id AS message_id,
       e->>'planner_call_ordinal'               AS ordinal,
       e->>'prompt_tokens'                      AS prompt_tokens,
       e->>'cached_prompt_tokens'               AS cached_tokens,
       e->'request_byte_split'->>'total_bytes'  AS total_bytes,
       e->'request_byte_split'->>'tools_bytes'  AS tools_bytes,
       e->'request_byte_split'->'message_bytes' AS message_bytes,
       e->>'finish_reason'  AS finish_reason,
       e->>'error_class'    AS error_class,
       e->>'error_message'  AS error_message
FROM chat_messages m, jsonb_array_elements(m.tool_calls) e
WHERE m.session_id = :session_id AND e->>'_kind' = 'llm_call_audit'
ORDER BY m.sequence_no, (e->>'planner_call_ordinal')::int;

-- Q2: ledger cross-check (one row per provider call)
SELECT created_at, prompt_tokens, cached_prompt_tokens
FROM token_usage_ledger
WHERE source = 'composer' AND session_id = :session_id ORDER BY created_at;

-- Q3: operation-level disposition
SELECT id, created_at, failure_code, result_json
FROM composer_async_operations
WHERE session_id = :session_id AND failure_code IS NOT NULL ORDER BY created_at;

-- Q4: planner-level dispositions
SELECT * FROM composition_rejection_events WHERE session_id = :session_id ORDER BY 1;
```

(Column names in Q2–Q4 follow the brief; verify them against the live schema
before running. The query shape is the instrument, so run it once on a
known-good tutorial session first and confirm it returns the two-call split
recorded in the brief's ground-truth table.)

Classification rule, applied per session:

| Signal | Where | Limit |
| --- | --- | --- |
| `failure_code = 'REQUEST_BYTES_EXHAUSTED'` | Q3 | 1 — server byte ceiling (no provider call made for that attempt) |
| `error_message`/reason `context_length_exceeded` | Q1 | 2 — provider window |
| `budget_exhausted` / `REPAIR_EXHAUSTED` | Q3, Q4 | 3 — turn/repair budget: **stop, different ticket** |

Deployment facts to record (not derivable from code — none of them is stored
in the repository): `composer_planner_max_request_bytes` as deployed (code
default 2 MiB, `src/elspeth/web/config.py:377`), the planner model and its
context window, the escape hatch model and its window. The repository has no
context-window setting at all (`grep -rn "context_window\|max_context"` over
`web/composer/` and `web/config.py` returns nothing), so the window must come
from the provider's documentation for the deployed model.

Bytes per token from the brief's ground truth: 274,506 B / 64,374 tok
= **4.26 B/tok** on call 2. At that ratio the 2 MiB ceiling is ≈ 492k tokens,
which is above every current planner-class window. **If that ratio holds in
the affected sessions, limit 2 (provider window) will fire long before limit
1 can**, unless the deployment lowered the ceiling. This is a prediction to
check against Q1/Q3, not a finding.

## 2. Code-side growth paths (step 5)

Each item cites the code that confirms it. "Confirmed" here means the code
has no bound, not that this path is the one the user hits — that needs §1.

### 2a. Cross-turn conversation projection — CONFIRMED UNBOUNDED

`src/elspeth/web/composer/planning_application.py:193`
`_freeform_planner_conversation_context` appends every user-authored history
entry and every non-empty assistant message, with no size, count or token
check. Its input is the whole session: `composer_turn.py:979` reads
`service.get_messages(session.id, limit=None, ...)`.

`additional_prior_user_requests_omitted` (`pipeline_planner.py:624`) is never
set by any caller — the only writes are the dataclass default and its own
`to_dict` (`grep -rn additional_prior_user_requests_omitted src/` → 5 hits, all
inside `pipeline_planner.py:574–654`).

Already-present invariant worth keeping: `pipeline_planner.py:648` requires
that any omission still retains **at least two** user requests ("an anchor
and a retained recent request"). A prior design intended *keep the first
request plus the most recent ones*; the Phase 2 policy should honour that
rather than replace it.

The context goes into message `user 2` (provider request), so in the 4A split
its growth shows as `message_bytes[2]`. Ground truth: 3,226 B on a two-turn
session.

### 2b. Escape hatch transcript — CONFIRMED UNBOUNDED, and duplicates 2a

`pipeline_planner.py:4193` `_engage_escape_hatch` replaces the message list
with `initial_hatch_setup` + the request + `transcript.provider_json`.

- The transcript is built from the same `records` (full session,
  `composer_turn.py:1502` → `AdvisorTranscriptHistory.from_records`) and
  `AdvisorTranscriptOwner.snapshot` (`advisor_transcript.py:769`) emits every
  event with no bound.
- `_recover_read_bodies` (`advisor_transcript.py:445`) **re-reads recorded
  `read_attachment` / `get_blob_content` bodies from blob storage** and
  inlines them. One large attachment read earlier in the session can put its
  whole body into the hatch request. This is a growth path the brief did not
  list.
- `conversation_context` is dropped from the hatch request **only** when it
  has no assistant messages and the transcript covers every prior request
  (`pipeline_planner.py:4201–4206`). Any session with assistant prose keeps
  both, so the hatch request carries the history twice.

The hatch only fires after a primary-model failure, so it is a candidate for
"fails late in a long session" specifically on turns that needed recovery.
In the 4A split it appears as a new large `user` message after the hatch
setup; distinguish it in Q1 by the message count change at that ordinal.

### 2c. Within-run continuation — CONFIRMED bounded only by the turn budget

Tool round trips are appended to `messages` within one `plan_pipeline` call
(e.g. `_retain_terminal_rejection`, `pipeline_planner.py:4240`). Nothing
trims them; the run's turn budget is the only bound. Ground truth: 55,379 B
over 8 messages on call 2 of a tutorial run. This path does not grow *across*
turns, so it explains a failure late in one run, not one that appears only in
long sessions.

### 2d. Authoring-aid ceilings — CONFIRMED present in code

`planner_authoring_aids.py:1143` schema evidence 96 KiB, `:1157` planner
contract 48 KiB, `:1158` discovery digest 28 KiB, `:1169` model catalog 8 KiB,
`:1170` expression grammar 8 KiB. Whether they hold in the affected sessions
is a §1 check: `message_bytes[1]` should be flat across calls (41,705 B in
ground truth).

### 2e. Byte ceiling placement

`pipeline_planner.py:3628–3630`: measured over the canonical
`{"messages", "tools"}` after cache marking, before the provider call, on
every call including hatch turns. Confirmed as the brief describes.

## 3. What Phase 1 still needs before Phase 2

1. Affected session ids (or the user's identity id) and read access to the
   deployment's sessions store, or the maintainer runs Q1–Q4 and pastes the
   raw output into §1.
2. The deployed `composer_planner_max_request_bytes`, planner model, hatch
   model.
3. With those: the limit named, the growing `message_bytes` index, and the
   turn/byte total at failure. If the limit is the turn or repair budget,
   this work stops here.
