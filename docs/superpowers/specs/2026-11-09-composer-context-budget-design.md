# Composer context budget: design

Date: 2026-11-09. Status: design approved in conversation; awaiting review of
this written spec before an implementation plan is written.

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

No composer request is rejected by the gateway for size. Before every
provider call the server either sheds context until the request fits two
ceilings, or refuses with a recorded, actionable reason.

The token ceiling is set by the operator. It is both a correctness limit
(the model's window is not known to ELSPETH) and a spend control.

## Approach

A deterministic budgeter runs before every composer provider call on both
paths. It is a pure function: it adds no provider call and writes no content
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

Two ceilings are checked before every composer provider call:

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

### Token estimate

`estimated_tokens = ceil(request_bytes / ratio)`, where `request_bytes` is
the canonical UTF-8 size of `{"messages", "tools"}` already measured for the
byte split.

- `ratio` is taken from the session's most recent successful composer call:
  its stored `request_byte_split.total_bytes` divided by its stored
  `prompt_tokens`. Both are persisted, so the ratio is reproducible.
- Before the session has such a call, a fixed fallback ratio of 3.0 bytes per
  token applies. It is lower than the one measured figure (about 4.3), so it
  over-estimates tokens and sheds early, never late.

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
| 3 | Escape-hatch transcript events (planner path) | oldest events removed from the transcript message |
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
  shed until the estimate with it restored would be at or below the low-water
  mark. The previously shed set is read from the previous call's stored
  shedding record, not from memory.
- Within a turn: a stubbed tool result stays stubbed for the rest of that
  turn. Hysteresis does not apply within a turn.

### Cache effect

Shedding alters messages after the fixed prefix, so the call on which
shedding engages loses the sliding cache marker's benefit on the conversation
tail. The prefix cache is unaffected. Shedding to the low-water mark in one
step keeps this to an occasional call.

## Components

- **`src/elspeth/web/composer/context_budget.py`** (new). Pure, no I/O.
  - Input: the labelled segments, the policy, the session's ratio, and the
    history indices shed on the session's previous call.
  - Output: the messages to send and a shedding record, or a refusal.
  - The same input always yields the same output, so the recorded
    `messages_hash` is reproducible.
- **Planner call site** (`pipeline_planner.py`, in `call_model_body`,
  immediately before the existing byte check). Labels the conversation
  context, the within-run continuation and the hatch transcript.
- **Compose-loop call site** (`service.py`, before each model call). Labels
  history and tool round trips.
- **Settings.** The three new settings and the changed default on
  `WebSettings`, through the settings-to-runtime contract.

Call sites only label segments. Every shedding decision lives in the pure
module.

## Recording

On every composer call record and in the public call envelope, beside
`request_byte_split`:

- the policy hash;
- estimated tokens before and after shedding, and the ratio used;
- history indices removed, tool-call ids stubbed, and hatch transcript event
  indices removed.

When nothing is shed the record says so explicitly; absence is not the
signal. `_LLM_CALL_PUBLIC_AUDIT_FIELDS` and every consumer the whole-tree
gates pin are updated together.

## Refusals

- If the protected segments alone exceed either ceiling, the server refuses
  before any provider call.
- The refusal uses a new planner/composer code, distinct from
  `REQUEST_BYTES_EXHAUSTED`, and names the ceiling (tokens or bytes) and the
  largest component from the byte split.
- The refusal is written to the operation record. Today a byte-cap refusal
  leaves no call record.
- If the provider still returns `context_length_exceeded`, it is recorded as
  today. There is no shrink-and-retry: a retry is an extra provider call on
  the chokepoint. The recorded estimate beside the provider's verdict tells
  the operator the ceiling or margin is wrong.

## User-visible behaviour

- When history was shed on a turn, the turn shows a quiet notice that earlier
  conversation is no longer visible to the composer.
- On refusal, the user sees the cause and the recourse (start a new session
  or shorten the request) instead of the generic `operation_failed`.

## Compatibility

Breaking configuration changes, to be stated in the changelog for the release
this lands in (release to be confirmed with the maintainer):

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

## Out of scope

- Model-written compaction of shed history.
- A separate token ceiling for the escape-hatch model.
- Shrink-and-retry after a provider `context_length_exceeded`.
- Reducing the size of the skill prompt, catalog block or tool definitions.
- A user-triggered context reset.
- The brief's Phase 1 records investigation. It remains worth doing to
  confirm which component grows in the affected sessions, and its replay is
  the brief's acceptance evidence; this design does not depend on its result.
