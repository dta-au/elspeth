# R14. A recovery persist after a mid-turn tool write erases the durable advisor block fact (pre-existing)

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | yes |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-23#2 |

## Finding

- **Status and severity:** confirmed, **medium**, pre-existing.
- **Location:**
  - `src/elspeth/web/sessions/routes/_helpers.py:997`: `_durable_completion_gates`.
  - Callers at `_helpers.py:3184/3342/3591`.
  - `composer/service.py:2736-2740`: the mid-turn `composer_meta`.
- **What is wrong:** `_durable_completion_gates` re-reads the current head to carry the gate envelope forward. After any mutating tool call, the head is a mid-turn `authoring_only` row whose `composer_meta` has no `completion_gates` key. `parse_completion_gates` returns None for it, and the recovery save persists `{'schema_version': 2}`, which means "no gates withheld". That contradicts the helper's own docstring and the window's invariant comment at `_helpers.py:2758-2759`.
- **Failure scenario:** R0 holds a blocked advisor fact. The next turn makes a mutating tool call (writing R1 without the key) and then hits `ComposerConvergenceError`, a plugin crash or a preflight failure. The recovery handler writes R2 with an empty envelope, and `/validate` stops withholding `completion_ready` for a graph no advisor has reviewed. `completion_ready` is enforced in `shareable_reviews/service.py:455` and `audit_readiness/service.py:646`. On a generic 502 with no recovery persist, R1 stays the head with the same effect.
- **Suggested fix:** Carry the turn-start `completion_gates` value into every `authoring_only` mid-turn row, or have the recovery handlers use the turn-start state record the route already holds instead of the moving head.
- **Sources:** be-23#2.
- **Verifier notes:** The normal turn-end path parses the turn-start record and is unaffected. The helper dates from 2026-08-02 (5166baab22).


## Source findings and verification

### be-23#2: Recovery persist after a mid-turn tool write erases the durable advisor block fact

- **Reported at:** `src/elspeth/web/sessions/routes/_helpers.py:997`; reviewer severity medium; category audit-integrity; diff-anchored False.
- **Summary:** _durable_completion_gates carries forward the current head's gate envelope. After any mutating tool call the head is a compose-loop mid-turn row whose composer_meta has no completion_gates key (service.py:2736-2740, stored verbatim by _insert_composition_state). The carry-forward therefore yields {'schema_version': 2}, which means 'no gates withheld', and the recovery save persists it. This contradicts the window's new invariant comment at _helpers.py:2758-2759 and the helper's own docstring.
- **Failure scenario:** The head row R0 holds a blocked advisor_signoff fact. The next compose turn makes a mutating tool call (mid-turn row R1 without the key), then hits ComposerConvergenceError, a plugin crash or a runtime-preflight failure. _handle_convergence_error (or a sibling handler) writes R2 with completion_gates={'schema_version':2}, and /validate stops withholding completion_ready for a graph no advisor reviewed. On a generic 502 with no recovery persist, R1 stays the head with the same effect. The old code lost the fact the same way, so this is not a regression.
- **Evidence:** composer/service.py:2736 composer_meta={'validation_lane':'authoring_only', ...}; sessions/service.py:6569-6581 inserts composer_meta verbatim; get_current_state orders by version desc; completion_gates.py:269-270 returns None for a missing key, and completion_gates_meta_from_facts(None) returns {'schema_version': 2}; _helpers.py:3184/3342/3591 pass that value as prior_completion_gates.
- **Suggested fix:** Carry the turn-start completion_gates value into each authoring_only mid-turn row. Alternatively, have the recovery handlers use the turn-start state record the route already holds rather than re-reading the moving head.
- **Verifier (trace):** upheld, confidence high, severity medium. I traced the path at 74c0ce0db and could not refute it. Mid-turn tool-call rows are committed to the database during the turn (one `persist_compose_turn` call per LLM round). Their `composer_meta` is built fresh and never includes `completion_gates`. The recovery handlers then look up the durable fact by re-reading the current head, which is now that mid-turn row, instead of the state the turn started from. `parse_completion_gates` returns None for the missing key, so the recovery row is saved with `{'schema_version': 2}`, which means "no gates withheld". That breaks the carry-forward promised in the helper's own docstring and in the ruling that "a green runtime preflight alone cannot clear it". The normal turn-end path is not affected: messages.py:216 and compose.py:135 parse the turn-start record. Only the recovery helper re-reads the moving head. The fact is not re-derived anywhere, so the loss is permanent until a later turn gets a new advisor verdict. `completion_ready` is enforced beyond display: shareable_reviews/service.py:455 and audit_readiness/service.py:646 both check it. Medium severity stands. The helper dates from 2026-08-02 (5166baab22), so this is not a regression in the window, and `diff_anchored=false` is correct.
