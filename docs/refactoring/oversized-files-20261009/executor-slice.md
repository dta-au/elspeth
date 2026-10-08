# Executor test decomposition

The second local slice separates the 7,432-line executor test module by coherent responsibility. Its input is first-slice commit `794531426a48ed6666bb68a0b690126fe58690d0`, based on main `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. No production behavior, policy, signature, scanner implementation, or streaming-owned file changes in this slice.

## Resulting responsibilities

| Module | Physical lines | Responsibility |
|---|---:|---|
| `tests/unit/engine/test_executors.py` | 3317 | Transform execution, error routing, terminality, batch dispatch, pass-through checks, and the existing re-raise security gate |
| `tests/unit/engine/test_gate_executor.py` | 1384 | Gate outcomes, routing, resolved-destination dispatch, and failure records |
| `tests/unit/engine/test_aggregation_executor.py` | 1793 | Buffering, flush lifecycle, journal restoration, triggers, and terminality |
| `tests/unit/engine/test_node_state_guard.py` | 832 | Node-state lifecycle, failure attribution, and telemetry |
| `tests/fixtures/executors.py` | 227 | Shared contracts, call recording, and executor-test construction helpers |
| `tests/unit/engine/test_executor_authority_refusal.py` | 73 | Existing leadership/membership refusal tests; only its helper import changes |

Counts use the inventory's LF-plus-final-unterminated-segment rule. The remaining original module contains substantial tests, not a forwarding shell. Every resulting executor module is below 5,000 lines. This does not close the overall backlog: [backlog.md](backlog.md) keeps the other 24 original authored targets above 5,000, the processor residual at 9,731, and the additional published streaming target explicit.

Shared helpers now have one owner in `tests/fixtures/executors.py`; suites and the external refusal consumer import the needed bindings explicitly. `_make_factory` and its late-bound `_AGGREGATION_LEADER` move together, and aggregation tests use that same constant. Domain-specific helpers stay with their test family. Existing helper implementations are preserved, including unused local helpers; this slice does not combine relocation with cleanup.

`TestReRaiseGuardPattern`, all four analyzer helpers, the handler floor, allowlist and process-boundary latch set remain at their existing selector. Transform/batch/pass-through tests retain the selectors referenced by signed rationale and other tests. The decision-tree documentation remains intact; only the opening module description is corrected to describe the new layout. No source hash or AST policy pin needed adjustment. Existing suppression comments are preserved within unchanged definitions; none is added.

## Ownership and capacity

The streaming manifest identified by SHA-256 `d7e8e437f730534880586c41e8687057f8bc80a4996e8bdf18605ecb4184dee1` contains 536 unique paths. All six code paths above are outside it. Astra also compared the candidate paths against complete main-to-published and main-to-repair changed-path sets, with positive/negative controls; intersections were empty. This does not release the Composer, Sessions, execution, coordination or related gate surfaces reserved in the backlog.

Streaming coordination messages were sent before validation. Host process inspection distinguished streaming's existing source-control process from unrelated project test suites. This lane used one `nice -n 19` serial pytest process at a time, with bounded timeouts and both worktree source roots exported. No broad suite, PostgreSQL suite, push or merge was started.

## Validation

Raw logs and exit files are retained locally under `.claude/lanes/oversized-files-20261009/executors/`. The baseline ran before source edits; after-checks ran against the frozen split source.

```text
baseline collection: 271 tests collected in 0.59s; exit=0
baseline execution: 271 passed in 17.26s; exit=0
after collection: 271 tests collected in 0.44s; exit=0
after execution: 271 passed in 14.13s; exit=0
```

The baseline selection was the original executor module plus `test_executor_authority_refusal.py`. The after selection was all four executor responsibility modules plus the same refusal consumer. Both use `-n 0`, so the security gate and all refusal parameterizations actually execute rather than relying on collection alone.

The controlled comparison preserved all 379 definition nodes (including 249 test definitions) byte-for-byte and AST-for-AST, plus the consumer's fixture decorators and test bodies. The full mapped collection multiset is identical: retained module 103, gate 53, aggregation 76, node-state guard 33, refusal consumer 6. Mapping changes only the eight moved classes' module paths; parameter IDs remain exact. Controls rejected assertion changes, deleted definitions, missing/duplicate items and changed parameter IDs. Four private mutant runs exited 1 with their intended parity failures; unchanged source passed.

Runtime identity checks verified 24 explicit imported bindings plus constant/closure ownership, including the shared aggregation leader. The real reflection inventory found no probe sites across the six changed files, with a positive one-site control and a negative zero-site control. Suppression-comment multisets and the error decision-tree prose are unchanged. Definitions moved into the fixture module intentionally acquire that module as their `__module__`; consumers use the same canonical function/class objects through explicit imports.

```text
affected whole-tree checks: 26 passed in 119.23s; exit=0
canonical Python inputs: 3841 files
before/after input dictionaries, including HEAD: identical
input inventory SHA-256: 40702828c8db45571c58ac9983a65268acff652e7985a89297e4e105fcff2cdf
Ruff check: exit=0
Ruff format --check: exit=0
```

The 26 checks comprise all mock-discipline tests (3), Python walker authority tests (11), five live-tree/refusal/shape-drift masquerade checks, the test `hasattr` gate (1), and template call-site restrictions/controls (6). The retained re-raise gate also executed within the 271-test focused selection. Source hashes were captured before collection and after the completed gate process using the canonical Python walker; all 3,841 entries matched. No result is inferred from progress dots or an unfinished process.

Independent Astra review is recorded in [executor-review.md](executor-review.md). The architectural split and review were separate from implementation. No gate baseline, assertion, marker, private scanner binding, production lookup target or policy was relaxed to accommodate movement.

## Reproduction

Run from the dedicated checkout, redirect each invocation to its own log, and record its exit code before reading results:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/engine/test_executors.py \
  tests/unit/engine/test_gate_executor.py \
  tests/unit/engine/test_aggregation_executor.py \
  tests/unit/engine/test_node_state_guard.py \
  tests/unit/engine/test_executor_authority_refusal.py

.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/test_mock_discipline_baseline.py \
  tests/unit/elspeth_lints/test_python_file_walker_authority.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_a_fresh_banned_site_is_never_silently_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_probe_shape_drift_fires_when_count_and_key_stay_unchanged \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_matching_occurrence_count_is_clean \
  tests/unit/test_no_hasattr_branching.py \
  tests/unit/plugins/infrastructure/test_template_call_sites.py
```

Collection comparison uses the same selection with `--collect-only -q`, mapping only the moved classes' module paths to their original module. The validation is bounded to this test/support decomposition; it is not evidence of full runtime, PostgreSQL, CI or operator signature clearance.
