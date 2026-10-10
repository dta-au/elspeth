# DAG test decomposition

This third local slice separates the 6,292-line DAG test suite into validation, construction, routing, and shared setup. Its input is `fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2`, on main base `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. No production, policy, plugin, signature, manifest, conftest, or scanner code changes.

## Responsibilities and remaining work

| Path | Physical lines | Responsibility |
|---|---:|---|
| `tests/unit/core/test_dag.py` | 2955 | Graph/source/sink validation, coalesce, dynamic schemas and schema compatibility |
| `tests/unit/core/test_dag_construction.py` | 1611 | Construction, accessors, deterministic identities, node immutability and initial multi-source cases |
| `tests/unit/core/test_dag_routing.py` | 1536 | Gate connections, route mapping, edges, branching, DIVERT and terminal-route validation |
| `tests/fixtures/dag.py` | 233 | Seven existing setup helpers and their four shared routing constants |

Counts use the inventory's physical-LF rule. Every resulting module is below 5,000 lines; the original remains a substantial validation suite. No tests are imported back as forwarding aliases. [The backlog](backlog.md) retains all original targets and the additional published streaming target, with 23 original authored files still above 5,000 and eight above 10,000. Processor tests remain unfinished at 9,731 lines.

The higher-priority eight are still reserved: six are literal members of the streaming list, while `src/elspeth/web/composer/state.py` and `tests/unit/web/composer/test_state.py` remain under the earlier broader Composer reservation. Their absence from the literal list is not an ownership release. This bounded unowned test family was selected instead of changing a reserved surface or migrating the processor helpers' recovery/integration/PostgreSQL consumers.

## Ownership and compatibility

Before edits, the root read the owner's checkpoint 72 and its canonical `streaming-owned-paths-20261008-538-post-caddy.txt`. Measured: 538 unique paths; SHA-256 `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`. The additions over the old list are exactly `CONTRIBUTING.md` and `tests/unit/helpers/test_composer_https.py`; none was removed. The four code paths above are outside this updated list. Candidate-overlap controls detect an added reserved path.

Controlled AST import inventories found no external direct consumers of the exact original test module. The new fixture owns all helper globals together. Its wrapper retains the original deep-copy routing transformations, generated-name behavior and deliberate updates to the caller's coalesce/gate settings. These bodies and mutation ordering are unchanged. Sixteen explicit consumer imports share the canonical helper objects; all four routing constants and the wrapper's helper/raw-factory bindings resolve in that same module. Shared helpers intentionally acquire the fixture module as their `__module__`.

All suites stay under `tests/unit/core`, inheriting the same parent fixtures. The existing `plugin_manager` fixture remains function-scoped; there were no original module fixtures or `pytestmark` assignments to redistribute. The cycle test's function-local NetworkX monkeypatch is unchanged.

The corpus-pinned selector remains exactly:

```text
tests/unit/core/test_dag.py::TestCoalesceNodes::test_union_collision_policy_binds_coalesce_config_and_identity_with_default_compatibility
```

The aggregation dual-schema test also stays at its existing location. The manifest, reserved architecture contract test, conftests and CONTRIBUTING are unchanged. No fingerprint, baseline, expected exception, assertion, marker or suppression was altered to make relocation pass.

## Evidence

Actual completed pytest results, with separate exit files:

```text
baseline collection: 130 tests collected in 0.12s; exit=0
baseline execution: 130 passed in 2.14s; exit=0
after collection: 130 tests collected in 0.20s; exit=0
after execution: 130 passed in 1.85s; exit=0
```

The complete mapped node-ID multiset is identical: 58 retained, 41 construction, 31 routing. Only the approved moved-definition module paths normalize to the original; the pinned selectors do not change. All 193 recursive class/function definitions, including 130 test definitions, preserve exact source bytes and location-free ASTs. Module-level executable state and constants are preserved across the resulting union.

Controlled parity checks accepted unchanged input and rejected assertion mutation, definition deletion, duplicate/missing collected items and parameter-ID changes. Four private mutant subprocesses exited 1 as intended. The real reflection detector reported zero sites across the four files, with a one-site positive and zero-site negative control. Existing suppression-comment multisets are unchanged.

```text
affected whole-tree checks: 20 passed in 98.02s; exit=0
canonical Python inputs: 3844 files
before/after input dictionaries, including HEAD: identical
input inventory SHA-256: e065c3440223e6ca97cbe791a655a00345edab44d4df59cf3685a32568e20b90
Ruff check: exit=0
Ruff format --check: exit=0
```

The 20 checks comprise all three mock-discipline tests, all eleven Python walker authority tests, five live-tree/refusal/shape-drift masquerade checks, and the test `hasattr` gate. Captures used the canonical Python walker before collection and after the completed gate process; all 3,844 source entries matched. The six production-template-only checks from the prior executor slice were not repeated because their inputs did not change. No result relies on progress output from an unfinished process.

Separate Astra architectural analysis selected the responsibilities, and [independent Astra review](dag-review.md) checked the implementation and completed results. The source and collection controls provide evidence beyond equal test totals; the focused execution includes the unchanged pinned and refusal cases.

## Reproduction and scope

From the dedicated checkout, redirect each invocation to a private log and record its exit code before reading results:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/core/test_dag.py \
  tests/unit/core/test_dag_construction.py \
  tests/unit/core/test_dag_routing.py

.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/test_mock_discipline_baseline.py \
  tests/unit/elspeth_lints/test_python_file_walker_authority.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_a_fresh_banned_site_is_never_silently_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_probe_shape_drift_fires_when_count_and_key_stay_unchanged \
  tests/unit/elspeth_lints/test_masquerade_gate.py::test_matching_occurrence_count_is_clean \
  tests/unit/test_no_hasattr_branching.py
```

Raw logs and exit files are retained locally under `.claude/lanes/oversized-files-20261009/dag/`. Collection uses the first selection with `--collect-only -q`. Root coordinated capacity, held during an observed streaming pytest, rechecked the host before testing, and ran one bounded `nice -n 19` serial process at a time. No full suite or PostgreSQL suite was run for this isolated test/support move. These results do not claim runtime-wide, CI or operator-signature clearance. No push or merge is authorized or performed.
