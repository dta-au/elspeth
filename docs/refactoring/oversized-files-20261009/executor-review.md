# Independent review — executor test decomposition

**Verdict: approved. No blocking or nonblocking defect findings.** The six-path split preserves definitions, collection, helper bindings and security refusals; completed focused and whole-tree gate evidence supports the bounded test-only change.

Reviewed base: `794531426a48ed6666bb68a0b690126fe58690d0`. Scope is `tests/unit/engine/test_executors.py`, the new gate/aggregation/node-state test modules, `tests/fixtures/executors.py`, and the helper import change in `test_executor_authority_refusal.py`. No production source, policy, signature, pin or scanner implementation changed. This reviewer wrote only this report, ran no pytest, and assessed completed root-lane test logs alongside independent source/AST, collection and runtime-import checks.

## Definition and collection parity

A full recursive comparison against the committed original preserves every class/function definition, including decorators, assertions, method-local helpers, parameterization and comments. The union of executable module-level statements also matches the original, excluding intentional docstring/import changes. Moving code did not silently duplicate or remove constants or setup statements. The external authority-refusal consumer's definitions and fixture decorator are identical; only its helper import owner changed.

Independent raw results:

```text
definitions_source_and_ast_equal=379
test_definitions=249
top_level_executable_state_equal=True
consumer_definition_parity=True
existing_import_target_parity=True
all_patch_call_AST_equal=2
current_source_hashes_match_frozen=True
```

The comparison correctly rejects a mutation lowering the retained re-raise guard floor from 70 to 69 and a deleted definition. Actual pytest collection evidence preserves the complete node-ID multiset after normalizing only the module prefix for classes moved to their three expected destinations:

```text
baseline test_executors.py=265
baseline authority_refusal=6
post test_executors.py=103
post test_gate_executor.py=53
post test_aggregation_executor.py=76
post test_node_state_guard.py=33
post authority_refusal=6
baseline_total=271
post_total=271
full_multiset_normalization_equal=True
```

The collection check rejects duplicate, deletion and changed-parameter-ID controls. Original executor classes and functions retain their names. No test objects are imported back into the old module, and the shared fixture module contains support definitions rather than collectible tests. The original had no module fixture declarations; the consumer's leader/member fixture remains unchanged.

## Binding and security behavior

All 24 explicit imports of shared support bindings across the five consuming test modules resolve to identical objects from `tests.fixtures.executors`. Shared helper globals point to that fixture module. In particular, `_make_factory`'s nested batch callback resolves the same `_AGGREGATION_LEADER` object that aggregation tests import, and `_blocked_item` resolves the aggregation module's sole `_JOURNAL_T0`. A helper impostor fails the identity check. Both production Python package paths resolved inside the reviewed worktree.

Both original patch calls are preserved exactly: the object patch for `FieldMapper.process` and `elspeth.engine.executors.gate.ExpressionParser`. The test move introduces no module-sensitive `__file__`, `__name__`, `globals()` or `locals()` behavior from the original definitions.

`TestReRaiseGuardPattern`, its four scanner helpers, its 70-handler floor, its exact process-boundary latch set, and the published selector remain in the substantive transform test module unchanged. The retained nominal batch-dispatch tests still satisfy the locations named in the signed engine rationale. The signed YAML, CONTRIBUTING selector, DAG manifest and masquerade baseline are byte-identical to the base. There is no source-hash or signature re-pin, imported-test facade, weakened refusal, or new lint suppression. Existing suppression text inside unchanged definitions is preserved rather than added to make this split pass.

The sole external helper consumer now imports directly from the real fixture owner. Its six leader/member cases still assert that authority refusals propagate as the same exception object and that cleanup does not write an unauthorized failed state. Their full bodies and parameterization are unchanged, and they are included in the recorded 271-case focused run.

## Measured resulting files

| Path | Lines | SHA-256 |
|---|---:|---|
| `tests/unit/engine/test_executors.py` | 3317 | `5f878e414c50e11b6b6dbbff7b2591e237b051d30ff1f4c5c25f037eee588642` |
| `tests/unit/engine/test_gate_executor.py` | 1384 | `227a0d85558cebf3957252a5a3e4bc295a6ae6b5e47c5672de4a5a737e500a69` |
| `tests/unit/engine/test_aggregation_executor.py` | 1793 | `b780c41e8acf4a0dd1132256c15ca6bedd13626609210a719809270841ef106a` |
| `tests/unit/engine/test_node_state_guard.py` | 832 | `5b37a4f54ecf5b3cbee8245a78d89858b1305ceb7f23b3bedf4e40f17e7c91b1` |
| `tests/fixtures/executors.py` | 227 | `50547b0a7c4a91d692af3ad6c937833f14a2b3659307d8a0245a4d347447e11d` |
| `tests/unit/engine/test_executor_authority_refusal.py` | 73 | `902d5191d1af2dff374e576af0e471ac13a81c66ee28b18a19faf4f61717a78c` |

The 7,432-line original is now split by executor responsibility, with all resulting files below 5,000 lines. Shared construction has one explicit owner; transform tests and the security latch remain substantive in the original module.

The independently read streaming reservation manifest has SHA-256 `d7e8e437f730534880586c41e8687057f8bc80a4996e8bdf18605ecb4184dee1`. Its 536 unique paths have an empty intersection with the six candidate paths. A positive control adding one actual reserved path to the candidate set detects that overlap. No streaming reservation is consumed by this slice.

## Execution evidence and limits

Completed root-lane logs and exit files under `.claude/lanes/oversized-files-20261009/executors/` were read directly:

| Selection | Completion | Exit |
|---|---|---:|
| Original executor module plus authority-refusal consumer | `271 passed in 17.26s` | 0 |
| Four resulting test modules plus authority-refusal consumer | `271 passed in 14.13s` | 0 |
| Original collection | `271 tests collected in 0.59s` | 0 |
| Resulting collection | `271 tests collected in 0.44s` | 0 |
| Root-selected whole-tree gates | `26 passed in 119.23s` | 0 |

The 26-check selection is all three mock-discipline checks, all eleven Python-file-walker authority checks, the one no-hasattr-branching check, all six template-call-site checks, and these five masquerade cases: `test_live_tree_has_zero_unbaselined_findings`, `test_self_test_a_fresh_banned_site_is_never_silently_green`, `test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green`, `test_probe_shape_drift_fires_when_count_and_key_stay_unchanged`, and `test_matching_occurrence_count_is_clean`. It does not represent the entire masquerade resolver suite. The retained re-raise latch also executes in the 271-case focused run.

The complete canonical Python-source dictionaries in `before.json` and `after.json` compare equal. The reviewer recomputed their source digest and checked the recorded HEAD:

```text
HEAD=794531426a48ed6666bb68a0b690126fe58690d0
source_files=3841
before_after_equal=True
source_inventory_sha256=40702828c8db45571c58ac9983a65268acff652e7985a89297e4e105fcff2cdf
```

No full repository suite, PostgreSQL contention suite, signed trust-tier verification or reserved Composer/authority analyzer refactor is represented by this review. This is a test-only relocation with unchanged helper and scanner bodies; broader future infrastructure changes require their own characterization and gates.
