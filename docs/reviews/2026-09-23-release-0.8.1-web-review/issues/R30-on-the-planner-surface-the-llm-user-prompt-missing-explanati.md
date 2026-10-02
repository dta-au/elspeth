# R30. On the planner surface, the `llm_user_prompt_missing` explanation promises query names the planner never receives

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-13#1 |

## Finding

- **Location:** `src/elspeth/web/composer/tools/generation.py:1782-1789`, `state.py:1970`, `:3837-3843`, and `pipeline_planner.py:2549-2560`.
- **Wrong:** The explanation says "the rejection names … each query left without a template". The planner's feedback attaches `entry.message` only for an allowlist of codes that excludes this one, so the query labels never arrive.
- **Fix:** Add the code to the planner detail allowlist behind the `not withhold_candidate_facts` guard, or rewrite the sentence.
- **Sources:** be-13#1.
- **Verifier notes:** The planner wrote the candidate itself and can work out the missing queries, so this is misleading teaching, not a blocked repair.


## Source findings and verification

### be-13#1: On the planner surface, the llm_user_prompt_missing explanation promises query names the planner never receives

- **Reported at:** `src/elspeth/web/composer/tools/generation.py:1789`; reviewer severity low; category stale-teaching; diff-anchored True.
- **Summary:** The record serves _LLM_USER_PROMPT_MISSING_EXPLANATION, which says 'The rejection names the node and, in multi-query mode, each query left without a template' (state.py:1970). The hunk comment says the planner's redacted repair turn sees only this record. The planner's feedback projection attaches entry.message as `detail` only for plugin_options_invalid, gate_condition_ignores_stated_threshold and the unproducible-output-fields code (pipeline_planner.py:2549-2560). The query labels exist only in that message, so they never reach the planner.
- **Failure scenario:** A plan_pipeline turn (the rootless freeform first turn, or a guided turn) emits a multi-query llm node with 4 queries, 2 of them without a template, and no node-level prompt_template. The repair feedback names node:<id> but not the queries, while the explanation says it does. The planner must recompute which queries are missing or guess. A plausible repair adds one node-level prompt_template, which satisfies the fallback rule but gives the template-less queries a generic user prompt nobody asked for. Otherwise the planner burns a turn on explain_validation_error, which returns the same static text.
- **Evidence:** pipeline_planner.py:2549-2556 is the detail allowlist tuple, and llm_user_prompt_missing does not appear anywhere in pipeline_planner.py. state.py:3837-3843 puts the query labels only in the message. generation.py:1783-1784 has the comment 'the planner's redacted repair turn sees only this record'.
- **Suggested fix:** Either add llm_user_prompt_missing to the planner detail allowlist, behind the same not-withhold_candidate_facts guard (the message quotes only the planner-authored node id and query labels plus the static FIX), or remove the 'rejection names ... each query' sentence and state the rule for finding the missing queries in the FIX.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. At the pinned commit 74c0ce0db, the query labels appear only in the rejection message, and the planner's feedback projection drops that message for this code. The static explanation served to the planner still says "The rejection names the node and, in multi-query mode, each query left without a template." On the planner surface that sentence is false. The FIX shares the problem: it refers to "the named queries' template", but the planner never receives those names. The path is reachable because `_validate_llm_prompt_roles_present` runs in ordinary state validation for every llm node. The finding lands in this window: commit 6211043bd ("every llm node carries both prompt roles") added it. One thing softens the impact. The planner wrote the rejected candidate itself, so it can work out which queries lack a template from its own tool arguments, and the component still names `node:<id>`. So the harm is misleading repair teaching and a possible wasted turn, not a blocked repair. Low severity is right.
