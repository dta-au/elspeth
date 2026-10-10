# Processor test decomposition: first safe slice

Five complete responsibility classes now live in
[`test_processor_transform_execution.py`](../../../tests/unit/engine/test_processor_transform_execution.py):
`TestExecuteTransformNoRetry`, `TestExecuteTransformWithRetry`,
`TestHandleTransformErrorStatusRoutedOnError`, `TestTelemetryEmission`, and
`TestRowUnionBranchLossTelemetry`. This groups transform attempt execution,
retry/error routing, and their telemetry behavior without changing assertions,
fixtures, production code, or signed configuration.

The original [`test_processor.py`](../../../tests/unit/engine/test_processor.py)
remains the real owner of shared construction and durable-token helpers and
183 collected tests. The extracted module explicitly imports eleven existing
helper/constant bindings. It does not import or re-export test classes.
The original remains 9,731 lines; reaching the eventual 5,000-line goal requires
further independently characterized slices. This slice removes the original
from the greater-than-10,000-line category; it does not complete the broader
repository decomposition.

## Baseline and scope

Implementation base: `9e481bc85a32ee15f808c5e2b16cab7634e57b8e` on
`refactor/oversized-files-20261009`. The target file was unchanged against both
published `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc` and streaming repair
`703cdd1674d01d422baa4d54f210105edccf498c`: both target-path `git diff`
commands produced no output and exited 0 before editing.

Scope is the two test modules and this evidence note. No runtime behavior,
manifest node locator, helper authority, module fixture, or signed metadata was
changed. The three manifest-pinned classes retain their original module path:
`TestTransformModeOutcomeOrdering`, `TestProcessRowMultiRowOutput`, and
`TestDurableSchedulerResumeDrain`.

## Measurements

The source comparison visits every nested function/class definition and hashes
both its complete original source span (including decorators) and its AST with
location attributes excluded. It compares the complete union of the two
resulting modules against the captured original, checks disjointness, and
requires that only the five named classes moved. Raw instrument output:

```text
definitions=349
test_definitions=213
source_and_ast_parity=true
baseline_items=226
after_items=226
moved_items=43
remaining_items=183
collection_parity=true
lines[test_processor.py]=9731
lines[test_processor_transform_execution.py]=1871
```

Collection comparison uses full pytest node IDs and multiplicity. Only the
moved module path is normalized; class names, method names, parameter IDs, and
all remaining paths are compared literally. Full original IDs and the explicit
per-item mapping are retained with the evidence, not inferred from source.

The instrument accepts unchanged source and rejects an assertion mutation and
removed definition. Collection controls reject a duplicate, a removed item,
and a changed parameter ID. A separately constructed helper impostor fails
the identity comparison. These controls do not mutate the measured worktree.
The same verifier was also executed against private mutated copies: assertion
change, duplicate collection item, deleted collection item, and changed
parameter ID each exited 1 with `source/AST definition drift` or
`collection drift`. The unchanged final worktree verifier exited 0.

An AST inventory through the repository's `iter_gate_sources` authority found
17 other modules importing 12 unique symbols from the original. All 17 import
successfully after extraction, and their 65 module-level imported bindings are
identical objects from the retained owner. Function-local imports remain
available. The eleven extracted-module helper/constant imports are also
identical objects; callable helpers retain the original `__module__`.

## Executed verification

Every pytest process used one worker (`-n 0`), explicit exported
`PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"`, and a matching pytest
`-o "pythonpath=$PWD/src $PWD/elspeth-lints/src"`. The collection driver added
`$PWD` to that pytest option so its external driver could resolve `tests`.
Runtime assertions confirmed both `elspeth.__file__` and
`elspeth_lints.__file__` resolved inside the worktree. Each process wrote a
private complete log and separate exit file. Results were read only after the
process exited:

| Check | Raw completion | Exit |
| --- | --- | --- |
| Original collection | 226 tests collected | 0 |
| Original processor tests | 226 passed in 13.01s | 0 |
| Resulting modules collection | 226 tests collected | 0 |
| Resulting modules tests | 226 passed in 12.82s | 0 |
| Whole-tree checks below | 19 passed in 71.02s | 0 |
| Earlier frozen whole-tree mock repeat | 3 passed in 10.94s | 0 |
| Final integration whole-tree mock repeat | 3 passed in 20.51s | 0 |
| Ruff check, both modules | All checks passed! | 0 |
| Ruff format check, both modules | 2 files already formatted | 0 |
| Final definition/collection/identity instrument | parity true; hashes unchanged | 0 |
| Scoped `git diff --check` | no output | 0 |

The nineteen checks comprise all three mock-discipline tests, all eleven
Python walker authority tests, and five masquerade checks: the actual live-tree
baseline comparison, fresh-site rejection, covered-site red/green control,
shape-change rejection at unchanged key/count, and matching-count acceptance.
This is a bounded test-only move; no full suite or PostgreSQL runtime suite was
run, and the full masquerade resolver test suite was not rerun.

The executed test selections were equivalent to these commands from the
worktree root; every invocation redirected both streams to its named private
log and recorded `$?` separately:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"

# Before extraction:
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 tests/unit/engine/test_processor.py

# After extraction:
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 tests/unit/engine/test_processor.py tests/unit/engine/test_processor_transform_execution.py

# Affected whole-tree checks, including live scan and controls:
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/test_mock_discipline_baseline.py \
  tests/unit/elspeth_lints/test_python_file_walker_authority.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_a_fresh_banned_site_is_never_silently_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_probe_shape_drift_fires_when_count_and_key_stay_unchanged \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_matching_occurrence_count_is_clean

# Final repository-wide mock repeat:
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 tests/unit/test_mock_discipline_baseline.py

.venv/bin/python -m ruff check --no-cache tests/unit/engine/test_processor.py tests/unit/engine/test_processor_transform_execution.py
.venv/bin/python -m ruff format --check --no-cache tests/unit/engine/test_processor.py tests/unit/engine/test_processor_transform_execution.py
```

The inventory lane added `docs/refactoring/oversized-files-20261009/count_tracked_lines.py`
during the earlier whole-tree run. Consequently its mock result was superseded
by a repeated three-test mock gate after all Python inputs were frozen.
Integration review then corrected that inventory producer's CSV writer to
use `lineterminator="\n"`, keeping the generated inventory's line endings
consistent with the repository. The root lane repeated the three-test mock
gate after that correction with another equal before/after source capture:
`3 passed in 20.51s`, exit 0. This final integration result supersedes the
earlier mock results; their logs remain retained. Processor code and its
definition, collection, and helper-identity results did not change.
Runtime gate authorities reported:

```text
masquerade_SCAN_SUBDIRS=('src/elspeth', 'tests', 'scripts', 'elspeth-lints/src')
walker_PINNED_ROOTS=('elspeth-lints/src', 'scripts/cicd', 'tests')
```

The documentation script does not enter either of those two scans. Their
source roots changed only through the characterized processor extraction;
the processor source hashes stayed identical throughout verification.
The final repository-wide mock gate includes that script. Full canonical
Python-source snapshots taken immediately before and after the final gate
reported:

```text
source_files=3837
source_inventory_sha256=a12483571c3b8a30aab753039bceaae3be22404303a3f4611a7d1b4c1a03f9d8
frozen=YES
head=9e481bc85a32ee15f808c5e2b16cab7634e57b8e
```

Both full snapshot dictionaries, including HEAD, are equal. Final source hashes:

```text
tests/unit/engine/test_processor.py
63d339df6114f3934d97a70e5f6772945777870cd1eefc5ed31f9ee932686572
tests/unit/engine/test_processor_transform_execution.py
1914bd575b9d85f2131781231e7700c59ef4cfee57f2298f9dd0f09beb3e6f75
```

## Signed rationale locator

The human-readable rationale at `config/cicd/enforce_tier_model/engine.yaml:1037`
contains this historical locator:

```text
tests/unit/engine/test_processor.py::TestExecuteTransformWithRetry::test_shutdown_during_backoff_diverts_with_last_attempt_state_id
```

Its executable successor is:

```text
tests/unit/engine/test_processor_transform_execution.py::TestExecuteTransformWithRetry::test_shutdown_during_backoff_diverts_with_last_attempt_state_id
```

The assertion body is byte-identical and the successor passed in the resulting
module run. Repository reference search found no machine `test_ref` to the
moved classes. The signed YAML remains unchanged; architecture review confirmed
that documenting this relocation needs no compatibility re-export.

## Evidence and remaining work

Private reproducible evidence lives in
`.claude/lanes/oversized-files-20261009/processor/`: `original.py`,
`baseline-ids.json`, `after-ids.json`, `node-id-mapping.json`,
`definition-fingerprints.json`, `consumer-imports.json`,
`consumer-identities.json`, `parity.json`, `verify_parity.py`,
`capture_collection.py`, `capture_tree.py`, complete logs and exits, and
`final-gate-before.json` / `final-gate-after.json`. These are ignored lane
artifacts and must not be staged. This tracked note records the implementation
and measured results.

The ignored scripts are retained locally for replay and are not part of the
commit. With `lane=.claude/lanes/oversized-files-20261009/processor`, the
after-collection invocation uses
`PROCESSOR_COLLECTION="$lane/after-ids.json" .venv/bin/python "$lane/capture_collection.py" -o "pythonpath=$PWD/src $PWD/elspeth-lints/src $PWD" -n 0 --collect-only -q tests/unit/engine/test_processor.py tests/unit/engine/test_processor_transform_execution.py`.
Definition, collection, and identity verification uses
`.venv/bin/python "$lane/verify_parity.py" "$lane" "$PWD"`.
Full mock-scan source snapshots use
`.venv/bin/python "$lane/capture_tree.py" "$lane/final-gate-before.json"`
before the gate and the corresponding `final-gate-after.json` afterward.
`negative-control-exits.json` records the four deliberate verifier failures.
The final integration captures and mock log/exit live one level above the
processor evidence directory:
`.claude/lanes/oversized-files-20261009/integration-before.json`,
`integration-after.json`, their `.txt` summaries, and
`integration-mock.log` / `integration-mock.exit`. Both integration captures
were read and compared equal; the final log and exit file confirm the
20.51-second passing result reported above.

No execution blocker remains for this slice. Further decomposition below
5,000 lines is deliberately outside the first approved slice. No commit, push,
or merge was performed by the implementation lane.
