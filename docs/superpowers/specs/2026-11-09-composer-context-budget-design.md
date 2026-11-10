# Composer context budget: design

Date: 2026-11-09. Status: built for 0.8.13. This document was approved as a
design and has since been brought into line with what was built; the
differences are listed under "Changes from the approved design" at the end.

This design answers the Phase 2 questions of
[`docs/development/composer-context-budget-brief.md`](../../development/composer-context-budget-brief.md).
The constraints in that brief are binding here and are not repeated in full.

## Problem

Web Composer requests to the LLM gateway grow without bound. The maintainer
reports users reaching 2 MB+ requests every few days, with two symptoms:

- the server refuses the request at `composer_planner_max_request_bytes`
  (`REQUEST_BYTES_EXHAUSTED`, default 2 MiB); and
- the gateway rejects the request for too much context
  (`context_length_exceeded`).

Both are one defect: nothing in the composer sheds context.

Facts read from the tree at `756efc01`:

- No truncation, compaction, summarisation or history window exists in
  `src/elspeth/web/composer/`.
- The byte check exists only on the planner path
  (`pipeline_planner.py`, before each provider call). The compose loop
  (`service.py::_compose_loop`) has no byte or token check.
- The gateway's default body cap is 1 MiB
  (`gateway/src/elspeth_llm_gateway/core/config.py`, `max_body_bytes`), below
  the server's 2 MiB default.
- No setting or model profile records a context window, and the server has no
  tokenizer. The only token counts are the provider-reported `prompt_tokens`
  stored per call.
- Four inputs grow without bound: prior chat history (both paths), tool
  results accumulated within a turn, the planner's prior-request context
  (`planning_application.py::_freeform_planner_conversation_context`), and the
  escape hatch's full-session transcript
  (`pipeline_planner.py::_engage_escape_hatch`).

Not measured for this design: the per-component growth in the affected
users' sessions (the brief's Phase 1). The design therefore bounds all four
growth paths rather than the one that a records investigation might single
out.

## Goal

No compose-loop or planner request is rejected by the gateway for size.
Before each of those provider calls (the escape hatch and the reply stage
included) the server either sheds context until the request fits two
ceilings, or refuses with a recorded, actionable reason. Calls this does not
cover are listed under "Not covered".

The token ceiling is set by the operator. It is both a correctness limit
(the model's window is not known to ELSPETH) and a spend control.

## Approach

A deterministic budgeter runs before every compose-loop and planner provider
call. It is a pure function: it adds no provider call and writes no content
of its own, so it stays inside both composer invariants (the planner does the
job; no tutorial-special path). It is server-side redaction of what is sent,
which the invariants permit.

Rejected alternatives:

- **Model-written compaction.** Adds a provider call on the latency
  chokepoint, and a model summary would stand in for user-authored requests.
  Out of scope; a separate change needing the maintainer's ruling.
- **Shrinking at the source only** (per-result caps, a clearer refusal).
  Leaves history unbounded, so long sessions still fail.

## Budgets and settings

Two ceilings are checked before every compose-loop and planner provider call:

| Ceiling | Setting | Default |
|---|---|---|
| Prompt tokens | `composer_context_max_prompt_tokens` (new) | none; required |
| Request bytes | `composer_planner_max_request_bytes` (existing; now also enforced on the compose loop) | 960 KiB (was 2 MiB) |

Shedding marks, as fractions of the token ceiling:

| Mark | Setting | Default | Meaning |
|---|---|---|---|
| High-water | `composer_context_shed_above` (new) | 0.85 | shedding starts when the estimate exceeds this |
| Low-water | `composer_context_shed_to` (new) | 0.60 | shedding continues until the estimate is at or below this |

Validation: `0 < shed_to < shed_above <= 1`. One token ceiling applies to the
planner model and the escape-hatch model.

The byte ceiling is an outer guard with no marks of its own: if the request
exceeds it, shedding runs in the same order until the request is under the
byte ceiling and at or below the low-water token mark.

### The measure

Both ceilings are judged on one measure: the wire bytes of the request as it
will be sent. That is the canonical UTF-8 JSON of `{"messages", "tools"}`
with cache markers applied, so it counts message content, tool-call
arguments, replayed thinking blocks, tool definitions, JSON escaping and
framing. It is what a gateway's body cap sees, and everything the model
reads. Both call sites use one helper for it.

The stored `request_byte_split` is a different measure (a message with string
content counts as that content alone). Its meaning is unchanged and the
budget is not judged on it.

A sheddable segment's saving is counted in content bytes. Shedding never
saves less than that on the wire, so sizes computed after shedding are upper
bounds: an admitted request fits, and shedding may go further than strictly
needed. A fixed allowance covers the bytes a re-render adds (the omission
counts; the hatch transcript's gap marker).

### Token estimate

`estimated_tokens = ceil(request_wire_bytes / ratio)`.

- Each budgeted call records the wire bytes of the request it sent
  (`request_wire_bytes`). `ratio` is that figure divided by the call's stored
  `prompt_tokens`, taken from the session's newest successful budgeted call
  on the same model; failing that, from the newest successful budgeted call
  on any model. Advisor and auto-title calls are not budgeted calls and never
  supply the ratio.
- A measured ratio is clamped to 2.0-6.0 bytes per token. The ratio is
  provider-reported data steering a control decision, so a value outside the
  range is clamped, not trusted and not rejected. A ratio that is not a
  finite positive number is a programming error and raises.
- Before the session has such a call, a fixed fallback ratio of 3.0 bytes per
  token applies. It is lower than the one measured figure (about 4.3), so it
  over-estimates tokens and sheds early, never late. This includes the first
  turn of an existing session after the upgrade.

### Recorded policy

The four settings plus the fallback ratio are hashed into a policy hash
recorded on every call. Whether a request was reduced is a pure function of
persisted history, persisted call records and this policy. No per-process
flag or cache decides what the model sees.

## Segments and order of sacrifice

Each call site labels its messages as segments. A segment is protected or
sheddable.

Protected, never altered:

- the system skill prompt, the catalog block and the tool definitions (the
  cached prefix, byte-identical across calls);
- the current user message;
- the current pipeline state block;
- recovery and retained-operation messages;
- the most recent tool round trip of the current turn.

Sheddable, in this order, each oldest first:

| Order | Segment | How it is shed |
|---|---|---|
| 1 | Tool-result bodies from earlier round trips in the current turn | body replaced by a fixed stub; the tool call, its id and the result message stay |
| 2 | Prior assistant prose from earlier turns | message removed |
| 3 | Escape-hatch transcript events (planner path) | oldest events removed from the transcript message; only the event that refers to the current request is protected |
| 4 | Prior user requests from earlier turns | message removed |

Reasons: tool results are the bulkiest and can be fetched again by calling
the tool; assistant prose is model synthesis; user requests carry authority,
and an old restriction may govern a new instruction, so they go last.

### The stub

One constant string, the same for every tool, stating that the result was
omitted to fit the context budget and may be requested again. It contains
nothing derived from the result. Keeping the tool call and the result message
preserves the provider's call/result pairing.

### Telling the model that history was removed

When prior messages are removed, the request states how many. On the planner
path this populates the existing
`PlannerConversationContext.additional_prior_user_requests_omitted` (never
set today) and adds a sibling count for assistant messages. The compose loop
carries the same two counts. The counts are numbers only; the server writes
no recap.

### Stability

- Cross-turn: a history message shed on the session's previous call stays
  shed until the whole unshed request would be at or below the low-water mark
  and under the byte ceiling. The previously shed set is read from the newest
  stored call that carries a shedding record, not from memory.
- Whether more must be shed is judged on the request with the already-shed
  set removed, that is, on what would be sent if nothing new were shed. Only
  when that size is over the high-water mark or the byte ceiling does
  shedding continue, by kind and oldest first, to the low-water mark. A long
  session therefore sheds, holds its shed set while it grows through the
  band between the marks, and sheds again when the reduced request crosses
  the high-water mark.
- Within a turn: a stubbed tool result stays stubbed for the rest of that
  turn, whatever the size of a later request in the turn. The release rule
  above does not apply to it.

### Cache effect

Shedding alters messages after the fixed prefix, so the call on which
shedding engages loses the sliding cache marker's benefit on the conversation
tail. The prefix cache is unaffected. Shedding to the low-water mark in one
step keeps this to an occasional call.

## Components

- **`src/elspeth/web/composer/context_budget.py`** (new). Pure, no I/O.
  - Input: the request's wire size, the labelled segments, the policy, the
    ratio, the keys shed on an earlier call, and the keys held for the rest
    of the turn.
  - Output: which segments to shed and the sizes before and after, or a
    refusal. It knows nothing about message shapes.
  - The same input always yields the same output, so the recorded
    `messages_hash` is reproducible.
- **`context_budget_apply.py`** (new). The message-shape helpers both call
  sites share: the wire measure, the stub, labelling of tool results, the
  previously shed and held keys.
- **`context_budget_history.py`** (new). Reads the ratio and the previously
  shed history from the session's stored call records.
- **Planner call site** (`pipeline_planner.py`, in `call_model_body`,
  immediately before the existing byte check). Labels the conversation
  context, the within-run continuation and the hatch transcript.
- **Compose-loop call site** (`service.py`, before each model call). Labels
  history and tool round trips.
- **Reply stage** (`service.py`, the one optional call after review cards are
  staged). It advertises no authoring tools, so its request carries each tool
  record as quoted text. The loop's own messages are fitted first and the
  reply request is built from the fitted list, so results stubbed earlier in
  the turn stay stubbed and only the latest round is quoted in full. Nothing
  is prepared when the stage is out of time.
- **Settings.** The three new settings and the changed default on
  `WebSettings`, through the settings-to-runtime contract.

Call sites only label segments. Every shedding decision lives in the pure
module.

## Recording

On every budgeted composer call record and in the public call envelope,
beside `request_byte_split`, a `context_budget` record:

- `policy_hash`;
- `bytes_per_token`, the ratio the estimate used;
- `estimated_tokens_before`, the estimate of the unshed request;
- `estimated_tokens_after`, an upper bound on the estimate of the request
  sent (savings are lower bounds and the allowance is included);
- `request_wire_bytes`, the exact wire bytes of the request sent;
- `shed_history_indices`, `stubbed_tool_call_ids` and
  `shed_hatch_event_indices`.

When nothing is shed the record says so explicitly; absence is not the
signal. An advisor or auto-title call stores a null `context_budget`.
`_LLM_CALL_PUBLIC_AUDIT_FIELDS` and every consumer the whole-tree gates pin
are updated together.

## Refusals

- If the protected segments alone exceed either ceiling, the server refuses
  before any provider call.
- The refusal uses a new planner/composer code, distinct from
  `REQUEST_BYTES_EXHAUSTED`, and names the ceiling (tokens or bytes) and the
  largest component, sized per message on the wire.
- Both paths answer it with HTTP 422. The named cause is stored in the
  operation's sealed result body (`error_type` `context_budget_exceeded`,
  with the detail codes). The operation row's `failure_code` column is
  unchanged (`http_error`). A refusal leaves no call record, because no call
  was made.
- A refusal on an escape-hatch turn surfaces as itself, not as the
  exhaustion that engaged the hatch.
- The reply stage is the exception to "a refusal fails the turn". Its call is
  optional and the turn's work is already done, so a request that cannot be
  fitted is not sent and the turn completes without a reply. The refusal is
  recorded on the turn's audit trail as an `audit` row with its own envelope
  (`composer_reply_stage_refusal`, reason `context_budget_exceeded`): the
  ceiling, the estimated tokens and wire bytes, the configured ceilings and
  the largest component. Sizes and closed-vocabulary names only.
- If the provider still returns `context_length_exceeded`, it is recorded as
  today. There is no shrink-and-retry: a retry is an extra provider call on
  the chokepoint. The recorded estimate beside the provider's verdict tells
  the operator the ceiling or margin is wrong.

## User-visible behaviour

- When history was shed on a turn, the turn shows a quiet notice that earlier
  conversation is no longer visible to the composer.
- On refusal, the user sees the cause and the recourse (start a new session
  or shorten the request) instead of the generic `operation_failed`.
- When the reply stage is refused, the turn's message ends with one fixed
  notice that the work was completed but the reply could not be produced
  because the conversation is too large, with the same recourse. It does not
  offer a retry.

## Compatibility

Breaking configuration changes, stated in the changelog for 0.8.13, the
release this lands in:

1. `composer_context_max_prompt_tokens` is required. An existing deployment
   must set it before upgrading; the server does not start without it.
2. The default of `composer_planner_max_request_bytes` drops from 2 MiB to
   960 KiB. A deployment that needs the old value must set it explicitly and
   must raise the gateway's body cap to match.

Existing sessions keep working. A session already over the high-water mark
sheds on its next turn with the omissions recorded. No stored data is
migrated, reset or rewritten; call records gain fields, and older records
without them are read as "no shedding recorded".

## Testing

Unit tests on the pure module, over synthetic segments:

- below the high mark nothing is shed;
- the shed order is exact, oldest first within each segment class;
- shedding stops at the low mark;
- a byte-ceiling overflow sheds even when the token estimate is under the
  high mark;
- previously shed history stays shed until the low-mark condition holds;
- stubbing preserves tool-call and result pairing;
- identical input yields identical output and hash;
- protected segments over a ceiling yield a refusal naming the ceiling and
  component;
- the ratio falls back to 3.0 when the session has no successful call.

Integration, one per path: an oversized session sheds, the request sent is
under both ceilings, and the shedding record matches what was sent. Each has
a negative control: the same session overflows when the budgeter is bypassed
in the test.

Gates: the whole-tree gates named in the brief's Phase 4, the contracts
census (`python -m scripts.check_contracts`), and the keyless lint corpus
compared before and after. Schema or persistence changes to call records
also require the `testcontainer` suite.

## Not covered

The budget does not bound every provider call the composer makes, and the
estimate is not exact:

- Advisor and review calls (the END gate, the early checkpoint, the advisor
  tool) are not budgeted. They carry the whole session transcript and can
  still be rejected by the gateway for size, which leaves the advisor
  unavailable for that turn. Tracked as dta-au/elspeth#364.
- Tool-call arguments and thinking blocks count toward the budget but are
  never shed. A turn whose arguments alone cannot fit is refused.
- Token counts are an estimate from bytes and a measured ratio, not a
  tokenizer.
- There is no retry when the provider rejects a request for context length.
- Graph recovery, run diagnostics, auto-title and the boot probe are not
  budgeted; their requests are small and carry no history.

## Out of scope

- Model-written compaction of shed history.
- A separate token ceiling for the escape-hatch model.
- Shrink-and-retry after a provider `context_length_exceeded`.
- Reducing the size of the skill prompt, catalog block or tool definitions.
- A user-triggered context reset.
- Budgeting the advisor's transcript.
- Making old tool-call arguments sheddable.
- The brief's Phase 1 records investigation. It remains worth doing to
  confirm which component grows in the affected sessions, and its replay is
  the brief's acceptance evidence; this design does not depend on its result.

## Changes from the approved design

Decisions made while building and reviewing that changed what this document
first said:

- **The decision is fed the request as sent.** The first plan measured the
  request before cache markers and re-rendering, then refused if the applied
  request was over the byte ceiling, which could refuse a request that still
  held sheddable content. The decision now measures a marked copy plus a
  fixed allowance (Ruling 4).
- **One skill paragraph serves both paths.** It names the compose loop's
  omission counts as well as the planner's (Ruling 6).
- **Hatch transcript events.** Only the event that refers to the current
  request is protected; legacy tool rows are sheddable like any other event
  (Ruling 7).
- **A refusal on a hatch turn surfaces as itself** (Ruling 8).
- **Hysteresis is judged on the reduced size.** The approved text did not say
  which size the high-water mark is compared with; comparing the unshed size
  shed again on every call (Ruling 9).
- **One measure, wire bytes.** The approved design estimated tokens from the
  stored content split. That split leaves out tool-call arguments and
  thinking, so both ceilings and the ratio moved to the wire bytes of the
  request sent, recorded on each call as `request_wire_bytes` (Rulings 10 and
  13).
- **The ratio's source.** Budgeted calls only, the same model preferred,
  clamped; the approved design took it from any successful call (Ruling 11).
- **The reply stage** is fitted from the budgeted loop messages and degrades
  without failing the turn (Ruling 11); its refusal is recorded and has its
  own notice (Ruling 14).
- **HTTP 422 on both paths** for the refusal (Ruling 11).
- **Advisor calls are not covered** (Ruling 12).
- **Within-turn tool stubs are never released**, to keep the approved
  "stays stubbed for the rest of that turn" true at the reply stage.
