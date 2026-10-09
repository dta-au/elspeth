# Processor test decomposition completion

The continuation completes the original processor test module split on base
`c4eb6565ef725b39a283663ca2869df8e9eb6298`, retaining durable drain, recovery,
expansion ordering and shared helper ownership in the original. Complete row
lifecycle/aggregation classes and traversal/gate/coalesce classes move to two
substantial sibling modules. Every resulting processor suite is below 5,000
physical lines. The existing transform-execution suite remains byte-identical.

| Path | Physical lines | Collected tests | SHA-256 |
|---|---:|---:|---|
| `tests/unit/engine/test_processor.py` | 4134 | 48 | `f455e4596abdb9060c16230ec1ccf0ab1ce490637736fdd72ff21144dc0c085b` |
| `tests/unit/engine/test_processor_row_lifecycle.py` | 3173 | 82 | `e0217281ff60f29648245df01ea69571b6816219909eb76ccf376fbcf28a32bd` |
| `tests/unit/engine/test_processor_routing.py` | 2541 | 53 | `a52019b894657e73be8cd730fc562db337fa174c45c23fe3ed3fc9c28fd91bd2` |
| `tests/unit/engine/test_processor_transform_execution.py` | 1871 | 43 | `1914bd575b9d85f2131781231e7700c59ef4cfee57f2298f9dd0f09beb3e6f75` |

Line counts measure LF plus any unterminated final segment. The original capture
is 9,731 lines with SHA-256
`63d339df6114f3934d97a70e5f6772945777870cd1eefc5ed31f9ee932686572`.
Regeneration dispatches complete AST-owned class units and their preceding section
comments using the approved responsibility map. Necessary imports and accurate
module scope docstrings change; definitions retain exact source spans, decorators,
annotations, assertions, nested definitions and location-free ASTs. There are no
class reexports, forwarders, duplicated helpers, padding or suppression additions.

## Ownership and semantics

All fourteen top-level construction/durable-token helpers and three constant
assignments remain original-owned. `_make_processor.__kwdefaults__["scheduler"]`
is the original `_DEFAULT_SCHEDULER` singleton. Every helper has the original
module name and original module dictionary as its globals, including transitive
registration, adoption, and group-binding helper calls. The two new modules bind
21 explicitly imported helper/constant objects to that authority. The unchanged
transform suite keeps its original eleven imports.

The current eighteen existing consumer modules were actually imported: all 76
module-level helper bindings have canonical object identity, and the three
function-local import occurrences retain available original bindings and unchanged
source. The fifteen distinct externally imported original bindings remain present,
including production `BarrierJournalRestoreContext` and `_FlushContext`. The
verifier also compared 143 public global bindings to the original imported source.
No production, fixture, consumer, conftest, policy, scanner, or manifest file changes
are included. All 538 protected streaming paths remain untouched; direct
membership and tracked-diff checks use reservation authority SHA-256
`3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`.

All six corpus selectors remain in the original `TestTransformModeOutcomeOrdering`,
`TestProcessRowMultiRowOutput`, and `TestDurableSchedulerResumeDrain` classes. The
class selector preserves its complete collected membership. The manifest,
architecture registry, and signed engine rationale are byte-identical. The
historical signed shutdown locator's documented relocation from the first slice
still points to the unchanged transform-execution suite. No signed-policy edit or
signature clearance is claimed.

Exact source/AST parity covers 291 recursive residual definitions plus 58 in the
unchanged transform suite, or 349 combined. All three non-import executable
assignments retain source/AST equality and ordering; the new modules introduce
none. All 158 patch/object/setattr call ASTs preserve complete operands and
multiplicity. Their production targets and scheduler enqueue patch remain valid.
Suppression COMMENT-token multiplicities are equal. Parameterization decorators
are exact; none of the modules defines fixtures, module marks or plugin lists.
All three files remain siblings under the same engine conftest chain, whose root,
unit and engine files are byte-identical to HEAD. Runtime item-marker baselines
were not separately captured; marker preservation is supported by exact decorator
ASTs, unchanged module metadata and unchanged parent fixtures.

## Measured validation

```text
baseline collection: 226 tests collected in 0.11s; exit=0
baseline execution: 226 passed in 14.47s; exit=0
after collection: 226 tests collected in 0.31s; exit=0
after execution: 226 passed in 14.16s; exit=0
Ruff check / format --check / git diff --check: exit=0
controlled comprehensive verifier: exit=0
```

Root owned the single serial test process and checked host capacity. This lane
ran no pytest. The collected multiset matches after normalizing only the approved
class-to-new-module paths; test names and full parameter IDs remain exact. The
capture requires complete anchored IDs to agree with pytest's summary and checks
positive IDs, IDs containing spaces, and negative summary lines. Full original
and resulting IDs and the explicit mapping are in the private evidence.

Seven actual private verifier subprocess controls exit 1 for the intended reasons:
assertion mutation and deleted definition (`definition source/AST drift`), duplicated
definition (`duplicate definition`), duplicated executable assignment (`executable
source/AST drift`), and duplicate/missing/altered-parameter collection items
(`collection drift`). Collection controls update the fake summary count, ensuring
the multiset comparison catches the mutation rather than merely rejecting capture.
An impostor callable is rejected by the identity comparator. The real masquerade
inventory records zero sites across the three paths after a synthetic probe
positive 1 and direct-read negative 0 control. Both production source roots resolve
inside this worktree.

The final twenty affected whole-tree checks completed with the same inherited
two failures:

```text
whole-tree gates: 2 failed, 18 passed in 99.94s; exit=1
canonical Python sources: 3985 files
before/after dictionaries, including HEAD: identical
inventory SHA-256: be5201391f144f4e52a4c9efcf9d26568de90a48b95385cda427acb76cbf96ec
root final Ruff check / format --check / diff --check: exit=0
```

The failing nodes are `test_no_unspecced_direct_mock_constructors` (three calls
in the protected recovery-coordinator suite) and
`test_live_tree_has_zero_unbaselined_findings` (eleven findings involving the
protected mutation-fencing suite and its baseline). Root's controlled comparison
proves the complete eleven masquerade tuples and full three-call mock diagnostic
exactly equal the earlier rebased run and clean published `9f26833c`. This slice
adds no findings. Raw comparison evidence is
`processor-continuation/gate-comparison.json` in the private repository lane.
These inherited integration failures remain a limitation; no gate, baseline,
production or protected file was altered to clear them. No signed trust-tier
verification was performed or implied.

## Reproduction and supporting evidence

Run from the dedicated worktree, exporting both production roots, redirect each
command to a separate log, and record its exit explicitly:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/engine/test_processor.py \
  tests/unit/engine/test_processor_row_lifecycle.py \
  tests/unit/engine/test_processor_routing.py \
  tests/unit/engine/test_processor_transform_execution.py

.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/test_mock_discipline_baseline.py \
  tests/unit/elspeth_lints/test_python_file_walker_authority.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_a_fresh_banned_site_is_never_silently_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_probe_shape_drift_fires_when_count_and_key_stay_unchanged \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_matching_occurrence_count_is_clean \
  tests/unit/test_no_hasattr_branching.py

.venv/bin/python -m ruff check --no-cache tests/unit/engine/test_processor.py \
  tests/unit/engine/test_processor_row_lifecycle.py tests/unit/engine/test_processor_routing.py
.venv/bin/python -m ruff format --no-cache --check tests/unit/engine/test_processor.py \
  tests/unit/engine/test_processor_row_lifecycle.py tests/unit/engine/test_processor_routing.py
```

Collection adds `--collect-only -q`; the current-base baseline selected the original
and unchanged transform module. Parent logs and exit files are under
`.claude/lanes/oversized-files-20261009/processor-continuation/`. The private workspace
`processor-continuation/` contains original source captures, exact destination map,
definition fingerprints, frozen hashes, full IDs, `node-id-mapping.json`,
`parity.json`, `scope-check.json`, `unchanged-inputs.json`,
`negative-control-results.json` and each deliberately failing process log/exit.
`source_parity.py`, `verify_processor.py`, `run_controls.py`, and `regenerate.py`
are private reproduction instruments, not staged repository additions.

This is bounded test-only movement. Helper behavior and runtime persistence,
SQL/schema, locking, registration and contracts remain unchanged. No full suite,
PostgreSQL testcontainer, CI, operator signature, commit, push or merge success is
implied by this lane's evidence.
