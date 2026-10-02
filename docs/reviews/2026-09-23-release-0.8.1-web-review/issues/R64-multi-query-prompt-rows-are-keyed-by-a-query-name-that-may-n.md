# R64. Multi-query prompt rows are keyed by a query name that may not be unique

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Frontend workspace, inspector and stores |
| Review line | frontend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | fe-09#1 |

## Finding

- **Location:** `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx:196-209`.
- **Wrong:** Duplicate list-form names produce sibling rows with the same key.
- **Fix:** Key on `${index}:${name}`.
- **Sources:** fe-09#1.
- **Verifier notes:** Planner write paths reject duplicates. The reachable path is YAML import (`persist_invalid`). With prop-only rows, the result is mostly a React warning.


## Source findings and verification

### fe-09#1: Multi-query prompt rows keyed by non-unique query name

- **Reported at:** `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx:198`; reviewer severity low; category correctness; diff-anchored True.
- **Summary:** LlmPromptRoles uses key={name} for each query's row. Names from queryEntries can repeat: duplicate list-form names, or the positional '#<index>' fallback matching a real name.
- **Failure scenario:** The planner authors queries: [{name:'score',template:'A'},{name:'score',template:'B'}]. The plugin rejects duplicate names, but only through validation. The state still holds both entries and the inspector renders two siblings with key 'score'. React warns, and when the options change during repair it can reuse or drop the wrong row, so the inspector shows a stale per-query user prompt.
- **Evidence:** OptionRows.tsx:196-209 `queries.map(([name, entry]) => (<div key={name}>`; the positional label `#${index}` is at :164; multi_query.py:324-332 rejects duplicate names only at validation.
- **Suggested fix:** Key on position as well, e.g. queries.map(([name, entry], index) => <div key={`${index}:${name}`}>).
- **Verifier (trace):** upheld, confidence medium, severity low. The defect is real, but the finding names the wrong way to reach it. The stated scenario, where the planner authors duplicate list-form names and the state keeps both, cannot happen. Every planner write path validates the options before storing them, and duplicate names fail that check. The other trigger in the finding, a nameless entry whose '#<index>' label matches a real name, is refused the same way. Validation happens inside LLMConfig's model validator, which calls resolve_queries. The tool paths that run it and return a failure without storing the state are upsert_node, patch_node_options, splice_transform and set_pipeline. So neither duplicate case can come from the planner. There is still a way in: the YAML import route. It copies node options verbatim, never parses LLMConfig, and saves with preflight_exception_policy="persist_invalid", so an imported state with duplicate list-form query names is stored with is_valid=False. The inspector then renders two sibling rows with the same key. The effect is smaller than claimed. The rows hold no local state and get their content from props, so a plain update of [score, score] to [score', score'] matches by position and shows the right text. A stale leftover row needs an unusual change in the list shape, such as re-importing over the same node. What is certain is a duplicate-key warning from React on a state that is already invalid. The suggested index-plus-name key is cheap and correct. Severity stays low.
