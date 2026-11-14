# DeepSeek final battery, 0.8.12 tree vs this morning's baseline

Tree composer-0812 @ 846bc4b0e (clean tracked tree). Planner `openrouter/deepseek/deepseek-v4.1-flash`, advisor `deepseek-v4-pro`, real provider calls, isolated instance 127.0.0.1:8791 (stopped by pid file; port 8791 confirmed free afterwards; live service untouched; no `/execute`). Round `ds-final-0812`, 19 cases x 1 repeat, `--no-tripwire`, battery exit 0, driver summary `completed: 19` (that counts captured runs, not successful composes; see below). Raw: `.claude/lanes/skill-ab/deepseek-probe/final/` (`runs/ds-final-0812`, `server.log`, `battery.log`, `final.json`, `base.json`, `after1.json`), tap `data/deepseek-probe/provider-tap.jsonl` lines 314-352. Analysis scripts: `an_final.py`, `mk_report_tables.py` in the same lane dir.

## Read these first

1. **WORSE: error_routing FAILED (1 of 19; baseline and first-pass runs of this case completed).** The planner's second call returned a valid `emit_pipeline_proposal`; the server's credential-material guard refused the response (`credential_material_rejected`, category `token_shape`, surface `composer_planner_response`, HTTP 422, planner attempt outcome `internal_error`). I re-ran the repo's `CREDENTIAL_PATTERN_SPECS` over the tap response: the only match is the `credential_assignment` pattern, in the model's **reasoning text** ("...that trips. And one with password..."), not in the pipeline arguments or the generated CSV. The prompt asks for deals screened for "passwords, secrets or confidential material", so the model reasoned about password-shaped strings. n=1: I cannot say whether this recurs, but the guard refusing a response because of reasoning text is a product-level defect candidate (unverified whether the guard is meant to scan reasoning; I did not read that code path).
2. **The catalogue premise did not hold.** I started the server WITHOUT `PROBE_EXTRA_PLUGINS` (verified unset in the launching shell), but the first-call digest in the tap lists `batch_stats`, `batch_replicate` and `batch_experiment_compare` (1 mention each; the morning baseline and first-pass taps have 0). `deploy/elspeth-web.env` (read by the probe launcher, not by me) was modified at 17:31:51 today, after the morning baseline; I was refused permission to inspect it and did not. So the three "plugin-absent" cases were run WITH their plugins and are not comparable to the baseline's declined/substituted outcomes; see section 5. The plugin catalogue is a confound for the whole table, not only for those three.
3. Better (all measured, n=1): reasoning tokens 205,938 -> 100,388 (-51%); completion tokens 235,268 -> 125,208; provider calls 40 -> 39; repair rounds 2 -> 1; advisor calls 1 -> 0; advisor escape hatch 0 -> 0 (first pass: 2); stringified `pipeline` 0 -> 0 (first pass: 6); `invented_source` mentions in reasoning 620 -> 94. Some of the reasoning drop in three cases comes from the plugins now being available (section 5), so do not credit all of it to the skill text.

## Method and instrument

- One script (`an_final.py`) applied identically to three rounds. Attribution: each run's `messages.json` audit rows (`llm_call_audit.provider_request_id`) equal the tap's response id, so every tap call is assigned to a case without relying on line order. Per run: prompt/completion/reasoning tokens from the audit rows (provider usage), `invented_source` = `str.count` of the literal in the tap `reasoning_content` of that run's calls, stringified pipeline = an `emit_pipeline_proposal` call whose decoded `pipeline` argument is a `str`, rejections and repairs from `planner_attempt_audit` rows (repairs = attempts with phase `repair`; advisor escape hatch = phase `hatch`; advisor calls = audit rows whose model is `deepseek-v4-pro`), wall = first user message to the assistant result message in the operation (1 s timestamp granularity; failed run falls back to the last message), plus summed provider `latency_ms` (agrees with wall within 1 s per round).
- Rounds: **baseline** = `ds-tap-full-1` in worktree composer-tool-remediation (tap `.../composer-tool-remediation/data/deepseek-probe/provider-tap.jsonl` lines 2-60, 19 cases, run 09:46-10:34 today). **first-pass after** = `ds-after-skill-1` (this worktree, tap lines 0-71). **final** = `ds-final-0812`. The canary and the 2-call threshold_gate smoke are excluded from my totals (the final round has no canary).
- The error_routing refused call has no provider_request_id in the audit (status malformed_response, no tokens); I added its tap usage (57,419 prompt / 4,800 completion / 3,175 reasoning, 3 `invented_source`) to that case. After that, final totals equal the sum over all 39 new tap lines exactly (2,149,710 / 125,208 / 100,388 / 94).
- **Instrument check against the recorded figures (partial).** Recorded: reasoning tokens 208,246 before vs 182,711 after; `invented_source` 861 vs 141. (a) `invented_source`: the same literal count over the baseline tap reproduces **861 exactly** for lines 2-61 (canary calls plus all cases, smoke excluded), and **141 exactly** for the first-pass tap lines 0-71. So the recorded counts include the canary repeats; my 19-case figures are lower (620 and 110) because the canary is excluded. Same instrument, different scope. (b) Reasoning tokens: **not reproduced.** No contiguous tap slice sums to 208,246 or 182,711 (I searched every slice of both taps). My 19-case attribution gives 205,938 (-1.1% vs 208,246) and 181,623 (-0.6% vs 182,711); the canary-inclusive baseline slice gives 232,933. I could not identify what the recorded figures included. The comparison below uses only my instrument for all three rounds; the recorded token figures are close to mine but are not the same measurement.
- Positive control: baseline slice reproduces 861. Negative control: `strp` is 0 on baseline (no stringified args existed) and 6 on first pass, where the report documents 5-6 stringified emits (3 in boolean_routing, 3 in llm_source), so the counter fires where it should and stays 0 where it should.

## Per-case: baseline then final

Columns: status; provider calls (including advisor); prompt, completion and reasoning tokens; repair rounds; `invented_source` mentions; compose wall seconds (indicative only: shared host, remote provider, prompt caching, a second tenant on the same box).

| case | base: st | calls | prompt | compl | reason | repairs | inv_src | wall s |
|---|---|---|---|---|---|---|---|---|
| batch_aggregation | ok | 3 | 181,054 | 11,850 | 11,079 | 0 | 20 | 56 |
| boolean_routing | ok | 2 | 103,575 | 6,862 | 6,029 | 0 | 39 | 17 |
| deaggregation | ok | 2 | 108,755 | 30,651 | 28,860 | 0 | 42 | 78 |
| deep_routing | ok | 2 | 107,709 | 19,185 | 16,098 | 0 | 68 | 40 |
| error_routing | ok | 2 | 105,529 | 8,512 | 6,333 | 0 | 10 | 19 |
| explicit_routing | ok | 2 | 103,572 | 8,216 | 7,350 | 0 | 46 | 22 |
| field_drop | ok | 2 | 104,559 | 3,280 | 2,560 | 0 | 17 | 10 |
| fork_coalesce | ok | 2 | 104,768 | 10,566 | 9,387 | 0 | 30 | 26 |
| json_explode | ok | 3 | 157,959 | 5,940 | 4,989 | 0 | 17 | 16 |
| llm_source | ok | 2 | 104,153 | 10,084 | 9,399 | 0 | 9 | 25 |
| multi_query_assessment | ok | 1 | 50,154 | 15,344 | 13,283 | 0 | 29 | 42 |
| openrouter_sentiment | ok | 2 | 109,335 | 12,286 | 10,212 | 0 | 40 | 32 |
| report_assemble | ok | 3 | 158,574 | 9,995 | 7,995 | 1 | 48 | 22 |
| row_union_ab_experiment | ok | 3 | 164,619 | 23,796 | 20,348 | 1 | 34 | 55 |
| schema_contracts_demo | ok | 2 | 103,632 | 6,845 | 5,611 | 0 | 31 | 16 |
| statistical_batch_plugins | ok | 2 | 104,802 | 8,622 | 7,479 | 0 | 61 | 21 |
| template_lookups | ok | 2 | 112,159 | 35,046 | 32,837 | 0 | 51 | 75 |
| threshold_gate | ok | 1 | 50,118 | 2,744 | 1,796 | 0 | 6 | 7 |
| transform_pipeline | ok | 2 | 105,964 | 5,444 | 4,293 | 0 | 22 | 13 |

| case | final: st | calls | prompt | compl | reason | repairs | inv_src | wall s |
|---|---|---|---|---|---|---|---|---|
| batch_aggregation | ok | 2 | 108,688 | 3,290 | 2,241 | 0 | 3 | 7 |
| boolean_routing | ok | 2 | 107,744 | 5,631 | 4,912 | 0 | 7 | 16 |
| deaggregation | ok | 3 | 166,862 | 3,685 | 1,959 | 1 | 3 | 13 |
| deep_routing | ok | 2 | 111,560 | 12,737 | 9,830 | 0 | 8 | 31 |
| error_routing | FAIL | 2 | 109,712 | 6,202 | 4,355 | 0 | 6 | 21 |
| explicit_routing | ok | 2 | 107,750 | 3,704 | 2,941 | 0 | 5 | 10 |
| field_drop | ok | 2 | 108,695 | 1,506 | 888 | 0 | 3 | 5 |
| fork_coalesce | ok | 3 | 168,660 | 15,913 | 14,691 | 0 | 9 | 41 |
| json_explode | ok | 2 | 107,983 | 3,016 | 2,327 | 0 | 0 | 9 |
| llm_source | ok | 2 | 109,157 | 3,937 | 3,217 | 0 | 5 | 11 |
| multi_query_assessment | ok | 2 | 114,992 | 10,460 | 8,247 | 0 | 5 | 28 |
| openrouter_sentiment | ok | 2 | 114,216 | 8,948 | 7,141 | 0 | 5 | 21 |
| report_assemble | ok | 2 | 107,973 | 4,008 | 3,054 | 0 | 4 | 10 |
| row_union_ab_experiment | ok | 2 | 112,288 | 13,993 | 12,402 | 0 | 12 | 33 |
| schema_contracts_demo | ok | 1 | 52,249 | 4,316 | 3,404 | 0 | 3 | 10 |
| statistical_batch_plugins | ok | 2 | 108,609 | 3,797 | 2,808 | 0 | 6 | 10 |
| template_lookups | ok | 2 | 114,728 | 15,133 | 12,862 | 0 | 3 | 44 |
| threshold_gate | ok | 2 | 107,732 | 2,088 | 1,178 | 0 | 3 | 12 |
| transform_pipeline | ok | 2 | 110,112 | 2,844 | 1,931 | 0 | 4 | 10 |

Rejections by code (planner_attempt_audit): baseline 2 (`batch_required_fields_invalid` x1 in report_assemble, `row_union_branch_alias_unreachable` x1 in row_union_ab_experiment); first pass 11 (`canonical_schema` x8, `plugin_options_invalid` x2, `sink_locked_extras` x1); **final 1 (`canonical_schema` x1, deaggregation)**. The deaggregation emit carried both `sources` and `source` keys (the oneOf shape the second-pass rejection text names); it was repaired in one round. The error_routing failure is not a planner rejection (it is the response-boundary refusal above).

## Totals (19 cases, n=1 each)

| measure | baseline | first-pass after | final |
|---|---|---|---|
| composes that completed | 19/19 | 18/19 (llm_source failed, REPAIR_EXHAUSTED) | **18/19 (error_routing failed, credential guard)** |
| provider calls (incl. advisor) | 40 | 51 | 39 |
| prompt tokens | 2,140,990 | 2,867,298 | 2,149,710 |
| completion tokens | 235,268 | 217,833 | 125,208 |
| reasoning tokens | 205,938 | 181,623 | 100,388 |
| repair rounds | 2 | 8 | 1 |
| rejections | 2 | 11 | 1 |
| advisor calls / escape-hatch attempts | 1 / 0 | 3 / 2 | 0 / 0 |
| stringified `pipeline` emits | 0 of 20 | 6 of 28 | 0 of 19 |
| `invented_source` in reasoning | 620 | 110 | 94 |
| sum of compose wall time | 591 s | 892 s | 342 s |

The baseline advisor call is the `deterministic_end_checkpoint` consult in batch_aggregation (the case the planner declined then); it is not the escape hatch. No `"verdict": "unavailable"` advisor outages were looked for in this round.

## Things that got worse or did not improve (measured vs baseline, same instrument)

- **error_routing: completed -> FAILED** (section above). The only completion regression.
- **fork_coalesce: reasoning 9,387 -> 14,691 tokens (+57%), calls 2 -> 3** (compose 26 s -> 41 s), no rejection, completed. One extra discovery-style call; I did not read why.
- **deaggregation: calls 2 -> 3, one repair** (`canonical_schema`, `source` plus `sources`), although reasoning fell 28,860 -> 1,959.
- **multi_query_assessment and threshold_gate: calls 1 -> 2** (baseline emitted on the first call; final does a discovery call first). Reasoning fell in both; listed because the call count went up.
- Prompt tokens are flat (2.141M -> 2.150M, +0.4%): the 0.8.12 text additions cost a small amount per call and the call count fell by one, so no net saving on the input side; only the output side fell.
- Compose wall time fell (591 s -> 342 s) but this is the least reliable measure here: one remote provider, other agents' load on the box, and the baseline ran in a different worktree's server at a different time of day.

## The three cases whose plugins were meant to be absent

Because the plugins were visible (section 2), the model used them. Outcomes from the emitted pipelines (tap `emit_pipeline_proposal` arguments), not counted as regressions or improvements:
- **batch_aggregation:** baseline declined (the earlier probe: no suitable plugin); final built a `batch_stats` aggregation node, 2 calls, 0 repairs, 7.7 s. Description text states five-at-a-time summaries as count, total and average per category.
- **deaggregation:** baseline was silently incomplete per the earlier trace findings; final built `batch_replicate` (twelve products, three at a time, numbered label rows to CSV), 1 repair (`canonical_schema`), no advisor.
- **row_union_ab_experiment:** baseline substituted per-row arithmetic; final built gate, two `value_transform` arms, `row_union`, `batch_experiment_compare` (the whole-stream comparison), 0 repairs.
This agrees with `deepseek-batch-plugins-probe.md` (9 of 9 used the plugins there, but with 2 of 3 deaggregation runs reaching REPAIR_EXHAUSTED; here the single deaggregation run did not). It cannot tell us what the model does when they are genuinely absent, which was the point of the brief.

## Limits

- n=1 per case, one model, one round, one provider route (OpenRouter; provider served per call is in the audit rows). Reasoning length varies several-fold between identical prompts on this model (first-pass vs baseline per-case swings of 2-10x occur in both directions); per-case movements are not evidence, only the totals are weakly informative, and even those are one draw.
- Different worktrees, hosts' load and time of day for baseline vs final; the served env also changed in between (plugin catalogue visible), so the comparison is not a clean A/B of the tree alone.
- Baseline tokens are not reproducible to the recorded 208,246 / 182,711 (see instrument check); 861 and 141 reproduce only with the canary included.
- I did not inspect the generated pipelines for correctness against each prompt, did not run or validate them beyond what the composer itself did, and did not accept proposals or resolve interpretation cards.
- Wall time has 1 s granularity and includes queueing/poll latency; indicative only.
- The error_routing credential refusal is one observation; I did not re-run it or read the guard's scope.
