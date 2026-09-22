# R27. The coalesce `on_error` rejection offers `quorum`, which the composer cannot author, and omits the `best_effort` timeout

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-10#1 |

## Finding

- **Location:** `src/elspeth/web/composer/state.py:3360-3373`.
- **Wrong:** The message tells the planner to ask the user to choose between `best_effort`, `quorum` and `first`. Stage 1 rejects `quorum` (`coalesce_policy_quorum_unsupported`, `:7845-7853`) and rejects `best_effort` without `timeout_seconds` (`:7865-7872`). **Scenario:** the planner can waste repair turns and offer the user an option that cannot exist.
- **Fix:** Drop quorum, or name it as unauthorable here, and say "best_effort with timeout_seconds".
- **Sources:** be-10#1.
- **Verifier notes:** recent-code-hints.md:95-96 records only the wording, and I think that entry is wrong on this point. `pipeline_capabilities.md:145-146` already teaches both constraints, so the cost is at most one turn.


## Source findings and verification

### be-10#1: Coalesce on_error rejection offers 'quorum', which the composer cannot author, and omits best_effort's timeout requirement

- **Reported at:** `src/elspeth/web/composer/state.py:3364`; reviewer severity medium; category stale-guidance; diff-anchored True.
- **Summary:** The rejection added by 1658d77de and 4e0d5d0d9 tells the planner to ask the user to choose between best_effort, quorum and first. The composer rejects quorum outright (coalesce_policy_quorum_unsupported). It also rejects best_effort without timeout_seconds (coalesce_best_effort_requires_timeout), and the message does not mention that requirement.
- **Failure scenario:** The planner tries upsert_edge(from=<coalesce>, type=on_error) and receives this rejection, then asks the user as told. If the user picks quorum, the planner sets policy 'quorum' and Stage 1 rejects it with coalesce_policy_quorum_unsupported. If the user picks best_effort, the planner sets it without timeout_seconds and Stage 1 rejects it with coalesce_best_effort_requires_timeout. The result is wasted repair turns and a user offered an option that cannot exist on this surface. This is the same class of defect as elspeth-032ec69c41, the problem this message was written to fix.
- **Evidence:** state.py:3371 says "'quorum' merges once the configured number of branches arrive ... ask the user which they want". state.py:7845-7853 rejects it: "policy 'quorum' requires quorum_count, which the composer cannot author. Use require_all, best_effort (with timeout_seconds), or first." state.py:7865-7872 raises coalesce_best_effort_requires_timeout. tools/generation.py:1059-1061 states quorum "cannot be authored here". recent-code-hints.md:1988 records quorum as unauthorable. recent-code-hints.md:95 only records the wording, which I think is wrong on this point.
- **Suggested fix:** Remove quorum from the offered alternatives, or name it as unauthorable in the composer. Say 'best_effort' needs timeout_seconds, for example "policy 'best_effort' with timeout_seconds". Keep 'first'.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at 74c0ce0db (HEAD verified). The coalesce on_error rejection in state.py:3360-3373 names 'quorum' as one of the policies to put to the user ("ask the user which they want"). It does not say quorum cannot be authored, and it does not say best_effort needs timeout_seconds. Stage 1 validation rejects both choices in the same file. The recorded ruling does not cover this. recent-code-hints.md:95-96 says only that the message "describes best_effort, quorum and first separately", which describes the wording. It does not rule that quorum should be offered. The older ruling at recent-code-hints.md:1988 still says quorum is unauthorable. No guard or normalisation stands between the planner and these outcomes. If the user picks quorum, the node gets policy='quorum' and state.py:7845 raises coalesce_policy_quorum_unsupported. If the user picks best_effort without a timeout, state.py:7865 raises coalesce_best_effort_requires_timeout. I lowered the severity to low for three reasons. Both rejections fire at Stage 1, with clear messages and repair hints (generation.py:1059-1061). The skill pipeline_capabilities.md:145-146 already teaches the planner that quorum is rejected and that best_effort needs timeout_seconds. The cost is at most one wasted repair turn plus a user who was offered an option that cannot exist on this surface. There is no correctness, data or audit impact.
