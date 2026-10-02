# R33. The corrupt-digest refusal applies only to `patch_node_options`, and `upsert_node` silently repairs the same corrupt digest

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-11#3 |

## Finding

- **Location:** `src/elspeth/web/composer/tools/transforms.py:1745-1752` and `interpretation_state.py:3188-3189,3261`.
- **Wrong:** Patching refuses while the stored `approved_prompt_artifact_hash` does not match. `upsert_node`, `splice` and `set_pipeline` delete the stored digest and write a fresh one. **Scenario:** with a stored `'a'*64`, a patch fails but an upsert succeeds and quietly writes a correct digest.
- **Fix:** Pick one policy for every entry point, and fix the comment at `:1745-1747`.
- **Sources:** be-11#3.
- **Verifier notes:** No unreviewed prompt runs, because per-requirement drift checks and the materializer re-derive the digest.


## Source findings and verification

### be-11#3: Corrupt-digest refusal only on patch_node_options; upsert_node heals the same corrupt approved_prompt_artifact_hash

- **Reported at:** `src/elspeth/web/composer/tools/transforms.py:1748`; reviewer severity low; category correctness; diff-anchored True.
- **Summary:** The new pre-reconcile check refuses any patch while the stored `approved_prompt_artifact_hash` does not match the options, so that 'an unrelated patch cannot heal a corrupt digest'. `upsert_node` cannot supply the key because it is runtime-owned, so its reconciliation deletes the stored digest and restamps a correct one. The invariant holds on one door only, and the refusal's catalogue fix sends the planner to a full re-emit, which is unguarded.
- **Failure scenario:** Stored digest 'a'*64 on an approved node: patch_node_options {temperature: 0.1} -> review_reconciliation_failed; upsert_node with identical options + temperature 0.1 -> success, digest restamped to the correct value.
- **Evidence:** Probe be-11-probe/probe.py case 3: '3a patch success False review_reconciliation_failed ...Stored approved_prompt_artifact_hash does not match' / '3b upsert success True' / '3b healed True'. The reconciler deletes and restamps the digest at interpretation_state.py:3188-3189 and 3261; the execution materializer re-derives it at 1326-1340.
- **Suggested fix:** Choose one policy: enforce the stored-digest check inside _reconcile_node_options for every door, or drop it from patch and rely on the per-requirement resolved-hash drift checks that already guard all doors. In either case, fix the comment at 1745-1747.
- **Verifier (trace):** upheld, confidence medium, severity low. I could not refute it. The stored-digest guard added in 5bc4b56c9 exists only in _execute_patch_node_options (transforms.py:1748-1752), and it compares only the target node (`current`). The shared reconciler that every other door calls deletes the stored `approved_prompt_artifact_hash` without checking it (interpretation_state.py:3188-3189). It then writes a freshly derived value whenever a resolved prompt review carries over (3261). As a result, upsert_node on the same corrupt node succeeds, keeps both reviews resolved, and writes the correct digest. The recorded test only covers the patch door (test_prompt_patch_reviews.py:180 test_patch_does_not_heal_corrupt_stored_approval); it pins no ruling that the other doors should differ. I reproduced the split myself at the pinned commit. The impact is real but small, so I kept severity low: (1) no normal path appears to leave a mismatched digest, so a bad one means first-party session data was corrupted or tampered with; (2) the digest is derived and is not the review evidence. The per-requirement hash-drift check (3211-3226) runs on every door, and the execution materializer derives the digest again (1331-1340 via _ensure_prompt_template_hash). So a successful upsert lets no unreviewed prompt through. The concrete defect is an invariant enforced on one door only, plus a comment at 1745-1747 that suggests a guarantee the other doors (upsert_node, splice, set_pipeline) quietly break.
