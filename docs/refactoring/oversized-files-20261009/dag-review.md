# Independent review — DAG test decomposition

**Verdict: approved. No blocking or nonblocking defect findings.** The four-path split preserves definitions, collection, helper bindings, fixture scope, patch behavior and the pinned corpus selector. Completed focused and whole-tree gate evidence supports the bounded test-only change.

Reviewed base: `fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2`. Scope: retained `tests/unit/core/test_dag.py`, new construction and routing test modules beside it, and shared `tests/fixtures/dag.py`. This reviewer changed no source and ran no pytest suite. Completed root-lane test evidence was read directly; independent checks covered complete source/AST definitions, executable module state, collected IDs, imports, runtime identities, fixture ownership, patching, pinned selectors and the active streaming reservation.

## Preservation checks

Every original recursive definition is present exactly once across the four files, with byte-identical source spans including decorators, assertions, local helpers and comments, and identical ASTs apart from location metadata. All non-import executable module-level statements also have the same multiset, so the four routing constants were moved rather than copied or omitted. Existing imported names retain their original targets. Only module descriptions and import ownership changed outside those preserved definitions.

Independent raw output:

```text
definitions_source_and_AST_equal=193
test_definitions=130
top_level_state_equal=True
existing_import_target_parity=True
monkeypatch_call_AST_equal=1
frozen_source_hashes_match=True
```

An assertion mutation and definition deletion correctly fail comparison. The original file captured for review is byte-identical to `git show <base>:tests/unit/core/test_dag.py`.

Actual before/after pytest collections preserve full node IDs and multiplicities, normalizing only the two destination module prefixes and requiring their expected top-level class/function names. Duplicate, deletion and changed-parameter-ID controls are rejected:

```text
baseline_total=130
post_total=130
post test_dag.py=58
post test_dag_construction.py=41
post test_dag_routing.py=31
full_multiset_parity=True
```

No `Test*` class or `test_*` function is imported back into the original module. `tests/fixtures/dag.py` owns seven construction helpers and four constants, with no collectible test or pytest fixture declaration.

## Helpers, fixtures, patches and pinned evidence

All 16 shared-helper imports in the three test modules resolve to identical objects from `tests.fixtures.dag`. All seven helper functions execute with that module's globals. The four `_AUTO_*` routing constants have one owner, and `instantiate_plugins_from_config` resolves the same raw wrapper and routing helper owned by that module. A distinct helper impostor fails the identity comparison. Both `elspeth` and `elspeth_lints` resolved inside the reviewed worktree.

The original test module defines no local pytest fixtures. All resulting test modules remain in `tests/unit/core`, so they inherit the same unchanged conftest chain. `plugin_manager` remains a function-scoped fixture in `tests/unit/conftest.py`; no fixture moves into a broader package scope or acquires a second registration authority. The root, unit and core conftest files are byte-identical to the base.

The complete NetworkX monkeypatch call remains `monkeypatch.setattr(nx, "topological_sort", raise_unfeasible)` inside the retained cycle-rejection test. Its function-local NetworkX import, failure function and `GraphValidationError` assertion are unchanged. Moving construction helpers does not redirect that patch or bypass the production graph constructor.

The parsed DAG corpus manifest still resolves this exact node ID in the post-split collection:

```text
tests/unit/core/test_dag.py::TestCoalesceNodes::test_union_collision_policy_binds_coalesce_config_and_identity_with_default_compatibility
```

A nonexistent-selector control does not resolve. The manifest and its contract test are byte-identical to the base; the coalesce class remains a real class in the original test module. The standalone aggregation schema tests also retain their original module location. CONTRIBUTING is unchanged. No production validator, runtime graph construction, corpus pin, signed policy or enforcement gate was edited.

The active streaming owner list has SHA-256 `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`. Its 538 unique paths have no intersection with the four candidate paths. Adding one actual reserved path as a positive control correctly detects overlap.

## Resulting files

| Path | Lines | SHA-256 |
|---|---:|---|
| `tests/unit/core/test_dag.py` | 2955 | `dcfa2815bb593a0825dc173b69b2cc29534c79122b1752eedc43cf419ad0a834` |
| `tests/unit/core/test_dag_construction.py` | 1611 | `82b552bb5e5a330ba596f8103348479f5b191e523f258db9e207a4204ea3f08f` |
| `tests/unit/core/test_dag_routing.py` | 1536 | `e491e6d38743893c07799c0c7d95eb448c9a910b25014b42bcf945c726fef981` |
| `tests/fixtures/dag.py` | 233 | `e7981f29ee261630f80ab5bec41241aeaf3f2ae4f5390854885eec4e53e273b2` |

The 6,292-line original is decomposed by construction/accessors, routing, and retained coalesce/schema validation responsibilities. Each resulting file is below 5,000 lines. Shared helpers have an explicit substantive owner; no thin compatibility facade or assertion-generating replacement hides unchanged size or weaker coverage.

## Executed evidence and limits

Completed root-lane logs and exit files under `.claude/lanes/oversized-files-20261009/dag/` were read independently:

| Selection | Raw completion | Exit |
|---|---|---:|
| Original DAG test module | `130 passed in 2.14s` | 0 |
| Three resulting test modules | `130 passed in 1.85s` | 0 |
| Original collection | 130 tests collected | 0 |
| Resulting collection | 130 tests collected | 0 |
| Selected whole-tree gates | `20 passed in 98.02s` | 0 |

The 20-check selection is all three mock-discipline checks, all eleven Python-file-walker authority checks, the one no-hasattr-branching check, and these five masquerade cases: `test_live_tree_has_zero_unbaselined_findings`, `test_self_test_a_fresh_banned_site_is_never_silently_green`, `test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green`, `test_probe_shape_drift_fires_when_count_and_key_stay_unchanged`, and `test_matching_occurrence_count_is_clean`. It does not represent the entire masquerade resolver suite.

The complete canonical Python-source dictionaries in `before.json` and `after.json` compare equal. The reviewer read the completed gate log and exit file, recomputed the source digest and rechecked all four current source hashes against the frozen candidates:

```text
HEAD=fb6c7185f90c8e94b0b03452d453e8cc8c0da3e2
source_files=3844
before_after_equal=True
source_inventory_sha256=e065c3440223e6ca97cbe791a655a00345edab44d4df59cf3685a32568e20b90
all_four_current_source_hashes_still_frozen=True
```

No full repository suite, PostgreSQL contention suite, signed trust-tier verification, production graph refactor or reserved streaming work is represented by this review. The reviewed change moves tests and exact helper bodies; future runtime or analyzer changes need their own broader characterization and gates.
