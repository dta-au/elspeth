# Compose cost measurement: cache fix verified live (2026-08-05)

Author: `claude-cache-measure`. Executes
`2026-08-05-allowlist-remediation-brief.md`; measures the fix from
`2026-08-05-compose-token-cost-addendum.md` (commit `6bcd69037`,
elspeth-a79f1b2e6b). Store queries ran read-only via the
`database-bootstrap` task; per-call evidence is the `llm_call_audit`
envelope in `chat_messages.tool_calls`.

## Deployment record

- `web:12` (brief's three-transform allowlist) was registered and
  doctor-validated but **never deployed**: the operator widened the scope
  mid-execution to "share all plugins unless there is a reason they are
  unavailable". It remains registered and inert.
- **`web:13` is the deployed revision**: env-only change from `web:11`
  (normalised diff proved exactly two values changed:
  `ELSPETH_WEB__PLUGIN_ALLOWLIST` 15 → 40 entries,
  `ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION` 11 → 13).
  Same image (`6bcd69037`), same schemas (SESSION_SCHEMA_EPOCH 45), tags
  replicated for the `ACCEPTANCE_RUN_ID` register condition. Doctor all-ok
  on the candidate TD; service stable 1/1/0 single PRIMARY/COMPLETED;
  `/api/health` and `/api/ready` 200. Rollback candidate: `web:11`.
- Allowlist now carries every installed plugin **except** seven with a
  concrete unavailability reason: `source:azure_blob`, `sink:azure_blob`,
  `transform:azure_content_safety`, `transform:azure_prompt_shield`,
  `transform:azure_document_intelligence` (no Azure credentials in this
  AWS stack; the two Azure guardrails would additionally force a
  `content_safety`/`prompt_shield` preference order naming an unusable
  control implementation) and `source:dataverse`, `sink:dataverse` (no
  Microsoft tenant). Boot safety was proven before registration by
  dry-running `compile_web_plugin_policy` against the real registry with
  the candidate allowlist: 41 authorized (40 + implicit `source:llm`),
  all identities valid. Live catalog confirms 6/29/6
  sources/transforms/sinks and no Azure/Dataverse leak.

## Composes (serial, `--no-run`, 280s client timeout)

| Graph | Result | Shape | Notes |
|---|---|---|---|
| g01 | **completed, 192s** (was 422 at 270s pre-allowlist) | `field_mapper` + `truncate` → csv; no `llm`, no guardrails | Sample-data trap checked: summaries 102–104 chars (truncate genuinely exercised); **but priorities were authored already-lowercase and no `value_transform` was wired** — see finding 1 |
| g02 | **completed, 84s** | `type_coerce` (`on_error → bad_rows`) + threshold gate → 3 json sinks | Sample includes an `N/A` junk score; quarantine genuinely exercised |
| g04 | 422 wall-clock timeout ×2 (271s both runs) | died mid-authoring | Both runs burned 5–7 turns on schema-contract repair loops (`guaranteed_fields` propagation, `flexible` vs `observed` mode); deterministic failure, not variance |
| g08 (same-shape probe) | 422 wall-clock timeout | LLM-shaped (two summary styles) | 5 completed calls + 1 killed; latencies 45+50+123+29+17+6 ≈ 270s |

## Cost result (verifies elspeth-a79f1b2e6b)

Cohorts were split by session identity (5 post-fix sessions on
web:11/web:13, all 2026-08-05), **not** by cache-key presence — see
finding 2. The pre-fix cohort reproduces the baseline exactly.

| | calls | sessions | total tokens | cache-read | hit rate | USD | USD/session |
|---|---|---|---|---|---|---|---|
| Pre-fix (web:10 era) | 168 | 17 | 10,908,494 | 4,054,780 | 37.2% | 22.7700 | **1.3394** |
| Post-fix (web:11/13) | 37 | 5 | 2,133,660 | 1,821,638 | **85.4%** | 2.1228 | **0.4246** |

- The ticket's estimate was "1.32 → ~0.40"; measured 1.3394 → 0.4246
  (−68%). Completed-compose mean (g01 + g02 only, excluding
  timeout-truncated sessions): $0.43.
- **Flattening curve confirmed** (`avg(total − cache_read)` by call
  index): pre-fix grows monotonically 26k → 40k → 48k → 59k → 76k
  (call 17); post-fix flattens 23k → 9.5k → ~8k → ~3k → <1k (call 11),
  with `cache_read` carrying the growing prefix (34k → 79k).
- `cache_creation_input_tokens` concentrates in each session's first call
  (avg 21.5k on call 1 vs ≤6.5k after), as predicted.
- **Graph-shape caveat, separated as required:** the dollar figure
  reflects both the cache fix and the cheaper deterministic graphs. The
  same-shape probe (g08, LLM-shaped) isolates the caching lever: within
  one LLM-shaped session, uncached input still flattened (59k first call
  building cache, then 3k/16.7k/8.8k/3.5k) where pre-fix same-index calls
  grew 26k → 40k. Caching works independent of graph shape; the remaining
  problem is latency, not cost.

## Task-3 verdict (one line)

Budget untouched; **diagnosed escalation** — g04 (schema-repair churn) and
g08 (LLM-shaped) still exceed the 270s wall clock post-allowlist, driven by
turn count × per-turn latency with observed 123s single-call thinking
tails; raising `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` or lowering
`ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT` (deployed default
`high`; knob live in image `6bcd69037` via elspeth-dc459d438e) are
operator decisions, surfaced here and not self-served.

## Findings

1. **The deterministic lowercase leg is inexpressible** (live confirmation
   of the 2026-08-05 case-folding finding): the composer read the
   expression grammar, found only `len()`/`abs()`, stated "string method
   calls like `.lower()` are not permitted in `value_transform`
   expressions", and pre-normalised its invented sample data to lowercase
   instead of wiring the transform. With real mixed-case data the intent
   still forces an `llm` node. The allowlist unblocks rename/truncate/
   coerce but does not close the case-folding gap.
2. **The brief's cohort discriminator was wrong against the live store**
   (premise re-derived, brief not trusted): all 205 audit rows carry the
   C6 cache keys — the round-3 build already wrote them. Additionally,
   the measurement query originally read `chat_messages.content`, which
   is the short `llm_call_audit_summary` projection and never carries
   cache fields on any build; the full projection nests under
   `tool_calls -> 0 -> 'call'`. `make_cache_cost_override.py` is fixed
   accordingly; cohorts must be split by session set (or deploy time),
   not key presence.
3. **Pre-fix caching was real but shallow**: 37% hit rate (tools+system
   prefix only) — consistent with the ticket's description that only two
   `cache_control` markers existed.
4. g04's failure mode is composer schema-contract churn
   (`guaranteed_fields` propagation through `field_mapper`/`json_explode`
   observed-mode schemas) — the same repair-loop class g01 survived and
   g04 did not. This is turn-count pressure, ticket material distinct
   from the latency knob.

## Addendum: effort-knob trial (web:14, same day)

Operator selected lever 1. `web:14` deployed (env-only:
`ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT=medium` added — the key
was previously absent, code default `high`; telemetry rev 14; doctor
green; 1/1/0; health/ready 200). Serial retrials:

| Graph | At `high` (web:13) | At `medium` (web:14) |
|---|---|---|
| g08 (LLM-shaped) | 422 at 270s (123s thinking tail) | **completed ~200s**, 5 reviews resolved; full-shape valid graph — both branches `prompt_shield → llm → content_safety`, input-side shield present, `value_transform` style tags |
| g04 (nested-JSON flatten) | 422 ×2 | still 422 at 271s — worst call 123s → 72s, but 7 turns of `json_explode` `guaranteed_fields`/list-typed-contract churn consumed the budget; binding constraint is turn count (elspeth-7da4e52344) |
| g01 (regression check) | 192s | 184s — no regression |

Verdict update: the effort knob resolves the latency half of the timeout
problem; the compose timeout stays untouched (as the brief's rule
demands). The residual g04-class failures are the freeform-loop
scaffolding defect, tracked on elspeth-7da4e52344.
elspeth-930a163c85 closed. Rollback candidate for web:14 is web:13.

## Addendum 2: g04 planner-route experiment (same day)

Following the elspeth-7da4e52344 leg-by-leg investigation (comments
2352–2354), the g04 topology was recomposed with a grammar-satisfying
rephrasing (`ops-local/acceptance/intents/g04p.json`; verified locally
that it classifies EXPLICIT_MUTATION → planner, while original g04
classifies AMBIGUOUS → compose loop).

**Result: the planner fails on this topology too** — two runs, both
HTTP 500 `composer_planner_failure` / `planner_repair_exhausted` at
127s/113s. Combined with the loop's n=2 wall-clock exhaustion, the g04
family defeats BOTH authoring surfaces deterministically; routing is not
the lever for this graph family.

The topology is nonetheless expressible: `examples/json_explode/
settings.yaml` (corpus green) authors it in 3 nodes with `items: any` +
`mode: observed` — no list declaration, no `guaranteed_fields`. The
schema DSL has no list field type (`contracts/schema.py:119`), so the
"prove `line_items` is a list" direction both surfaces pursued is a dead
end by construction. Classified as an authoring-guidance defect on
elspeth-7da4e52344 (comment 2356), sibling in shape to
elspeth-fcef029996, distinct from the elspeth-38dffd9bec expressibility
gap.

## Success criteria disposition

1. ✅ (as `web:13`, wider than briefed, on operator instruction)
2. ✅ g01 + g02 serial, trap checked (g01 credited with the finding-1 caveat)
3. ✅ figures + cache split + flattening curve + shape caveat above
4. ✅ diagnosed escalation (above)
5. ✅ this document; elspeth-a79f1b2e6b updated with the evidence
