# Brief: the Composer's per-compose token cost

Date: 2026-08-05. Author: `claude-r3-deploy`. Audience: the agent that reduces
it. Evidence source: acceptance battery round 3, release `release/0.7.2@90d5508fd`
deployed as `a-fa1b99c60192978b10f7-web:10`, 17 real compose sessions.

## The problem in one line

A compose that the maintainer previously measured at **under 10 cents** now
costs **~USD 1.34**, and the cause is a fixed ~49k-token prompt prefix plus a
schema carry-forward with a 96 KB ceiling — **not** the tool loop misbehaving.

## Measured facts

All figures are from `chat_messages` on the live acceptance session store
(role `audit`, `_kind: llm_call_audit`), not from estimates.

| | |
|---|---:|
| Compose sessions | 17 |
| LLM calls | 168 |
| Total tokens | 10,908,494 |
| Provider cost | **USD 22.77** |
| Mean per compose | ~10 calls, ~640k tokens, **~USD 1.34** |
| Worst single compose | 18 calls, 1.39M tokens, **USD 3.09** |
| Largest single call | 103,695 tokens |
| `reasoning_tokens` | **0 on all 168 calls** |

Split by model: Sonnet 151 calls / 10,583,513 tokens / USD 22.41; Haiku (the
advisor) 17 calls / 324,981 tokens / USD 0.36.

**The baseline is a constant.** The first Sonnet call of every session is
49,091–49,645 tokens — a ±0.6% spread across 17 independent sessions.

**The growth curve** (mean Sonnet tokens by call index, n = sessions reaching
that index): 49.3k (17) → 62.9k (17) → 66.5k (17) → 69.2k (17) → 72.1k (17) →
75.8k (15) → 78.2k (12) → 79.9k (11) → 84.2k (8) → 86.7k (6) → 88.2k (3).
The first step is the largest at **+13.6k**.

**Scale reference:** at Sonnet input pricing, 10 cents ≈ 33k tokens. The fixed
prefix alone is ~1.5× an entire ten-cent compose. **No amount of loop tuning
reaches the old figure while the prefix stands.**

## Where the 49.3k baseline comes from

| Component | Measured | ≈ tokens | Source |
|---|---:|---:|---|
| `SYSTEM_PROMPT` | 72,534 chars | ~19,600 | `web/composer/prompts.py:59` — `pipeline_composer.md` (57,871 B) with `pipeline_capabilities.md` (14,778 B) rendered in |
| Tool definitions | 41,314 chars, **43 tools** | ~11,200 | `web/composer/tools/_dispatch.py:265` `get_tool_definitions()` |
| Dynamic context, empty pipeline | — | **~18,500** (residual) | `web/composer/prompts.py:162` `build_context_string()` — message 2, "untrusted current state + plugin summary" |

The residual is arithmetic (49.3k − 19.6k − 11.2k − ~150 for the user intent),
not a direct measurement. **Measuring it directly is the first task below**,
because it is the least understood component and is present even before any
work happens.

Largest tool schemas: `set_pipeline` 8,537 chars, `upsert_node` 5,224,
`request_advisor_hint` 2,849, `request_interpretation_review` 2,256.
**16 distinct tools were used across the entire round; 43 are sent every call.**

## Where the growth comes from

`build_context_string`'s docstring (`prompts.py:174-187`) states the contract:
every schema identity the session has loaded is *"rehydrated through `catalog`
on every request into `schema_contract_evidence`"* — whole contracts, re-sent
each turn. That is why the largest single step lands immediately after the
opening `get_plugin_schema` burst.

It **is** bounded, and the bounds are the lever
(`web/composer/planner_authoring_aids.py:822-824`):

```python
_SCHEMA_EVIDENCE_MAX_ENTRIES:        Final[int] = 8
_SCHEMA_EVIDENCE_MAX_OMISSIONS:      Final[int] = 16
_SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES: Final[int] = 96 * 1024
```

**96 KB ≈ 26k tokens.** The observed growth from 49.3k to 88.2k is +39k, so
this budget alone can account for most of it. The mechanism arrived in
**`bb5a81213` (2026-08-04, "fix composer schema contract carry-forward")** —
394 added lines in `planner_authoring_aids.py`, 36 in `prompts.py` — **two days
before this battery**.

## Already ruled out — do not re-investigate

- **The skill file is not the regression.** `pipeline_composer.md` was *larger*
  in July (76,935 B at `efb30d6e2`, 2026-07-16), was cut to 52,124 B at
  `377bcc9a3` (2026-07-20), and is 57,871 B today.
- **Prompt caching is active and working.** `supports_anthropic_prompt_cache_markers`
  (`llm_response_parsing.py:786`) matches `bedrock/global.anthropic.claude-sonnet-4-6`
  through its `"claude-" in lowered` clause. Cost data confirms hits: call 2
  costs **less** than call 1 (USD 0.152 vs 0.176) while carrying 31% more
  tokens.
- **The loop is not bloated.** 6–12 assistant turns per compose at ~1.0–1.5
  tool calls per turn.
- **The repair path is not silent.** Validator errors return in full
  (`error_code`, `component`, and a `contract` block naming `producer`,
  `consumer`, `missing_fields`), and an `[ELSPETH-SYSTEM-HINT]` drift detector
  fires after three failed same-tool calls and demonstrably works — the next
  call succeeded with 12 affected nodes. It fired once in the whole round.
- **Reasoning is orthogonal.** `reasoning_tokens` is 0 on all 168 calls. The
  reasoning-effort feature (`elspeth-dc459d438e`) *adds* tokens; it pays only
  by removing turns.

## Constraints that will bite

Read these before touching either lever.

1. **Tool ordering is load-bearing for the cache.** Anthropic `cache_control`
   markers attach to the **last** tool (`_dispatch.py:319` and the
   `get_tool_definitions` docstring). The trailing entry is pinned to
   `wire_secret_ref` by `test_trailing_tool_name_is_locked`. **Reordering
   invalidates the cache for every follow-up turn** — a naive "sort tools by
   size" or "drop unused tools" change can make cost *worse* by destroying
   cache hits. Any trimming must preserve a stable prefix and a stable
   trailing entry.
2. **The skill enumerates the same tool set.** `pipeline_composer.md` lists the
   tools under "## CRITICAL: Tool Schema Availability", and drift is caught by
   per-tool prose tests in `test_skill_drift.py`. Removing a tool from the wire
   payload without updating the skill fails that gate — and worse, would leave
   the model believing in a tool it cannot call.
3. **The carry-forward fixed a real defect.** `bb5a81213` exists because
   progress reported "satisfied" while the schema was absent. **The question is
   its bounding, not its existence.** Do not delete it; a digest or a
   most-recently-referenced window keeps the guarantee at a fraction of the
   tokens.
4. **`get_tool_definitions()` is unconditional.** It takes no arguments and
   returns all 43 every time. Any gating is new API surface, so decide whether
   the gate is per-session (tools seen/likely) or per-phase (discovery vs
   authoring) before writing code.

## Suggested work, ranked

**1. Measure the ~18.5k dynamic context directly.** (Small, unblocks the rest.)
Instrument or unit-measure `build_context_string()` with an empty
`CompositionState` and the deployed 16-plugin policy. Confirm or correct the
residual. Report the split between untrusted-state and plugin-summary.
*Acceptance:* a number with provenance, replacing the arithmetic estimate.

**2. Trim the tool payload.** ~11.2k tokens on every one of 168 calls, with no
model-quality trade — 16 of 43 tools were used all round. Respect constraint 1
(stable prefix, pinned trailing entry) and 2 (skill + drift tests).
*Acceptance:* wire payload shrinks measurably; `test_trailing_tool_name_is_locked`
and `test_skill_drift.py` still pass; a compose still completes end to end.

**3. Bound the schema carry-forward harder.** `_SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES`
at 96 KB (~26k tokens) is the single largest tunable. Consider a digest form,
a most-recently-referenced window, or a materially lower byte budget.
*Acceptance:* the growth curve flattens; `bb5a81213`'s own tests
(`test_schema_contract_carry_forward.py`, 861 lines) still pass — they encode
the guarantee that must survive.

**4. Revisit `SYSTEM_PROMPT` size** (~19.6k) only after 1–3. It is the most
model-quality-sensitive component and the least safe to cut blind.

## How to measure before/after

Do **not** use wall-clock alone — it hides cost regressions, and compose
duration is independently noisy (43–272 s observed for comparable graphs).

Query the live session store read-only through the `database-bootstrap` task
definition; `ops-local/acceptance/make_inspect_override.py` and
`run_task.py` are the working harness. The three metrics that matter:

```sql
-- 1. Fixed baseline: first Sonnet call per session. Should be a tight band.
-- 2. Growth curve: mean tokens by call index.
-- 3. Cost per compose: sum(provider_cost) / count(distinct session_id).
SELECT count(*)                                              AS llm_calls,
       sum((content::jsonb ->> 'total_tokens')::bigint)      AS total_tokens,
       round(sum((content::jsonb ->> 'provider_cost')::numeric), 2) AS usd,
       count(DISTINCT session_id)                            AS sessions
FROM chat_messages
WHERE role = 'audit' AND content LIKE '%llm_call_audit%';
```

Note `chat_messages` holds four roles — `tool` 193, `audit` 170,
`assistant` 162, `user` 17 — and the messages API projects **only** `user` and
`assistant`. The audit rows and tool responses are invisible through the API;
go to the store.

**Target to argue against:** ~USD 1.34 per compose today. The maintainer's
recalled figure is under 10 cents. Whether that is reachable without a
model-quality trade is an open question this brief does not settle — but the
49.3k fixed prefix bounds it, so establishing what the prefix *must* contain is
the real deliverable.
