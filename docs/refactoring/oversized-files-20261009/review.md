# Independent review — first oversized-file slice

**Verdict: approved for the bounded processor-test extraction, inventory, and architecture proposal. No blocking or nonblocking defect findings.** This does not approve unimplemented decompositions, certify signed allowlists, or claim that the repository-wide size target is complete.

Reviewed branch: `refactor/oversized-files-20261009`, based on `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`. Review covered both processor test modules, the complete inventory script and its fixed-snapshot data, the architecture proposal, and the implementation evidence. The reviewer changed no source and ran no pytest suite; test results below are completed implementation and integration runs whose logs and exit-code files were read independently. Independent checks used full-file AST/source comparisons, runtime helper imports, parsed collection evidence and Git blob inventories.

## Implementation findings

The five moved classes are `TestExecuteTransformNoRetry`, `TestExecuteTransformWithRetry`, `TestTelemetryEmission`, `TestRowUnionBranchLossTelemetry`, and `TestHandleTransformErrorStatusRoutedOnError`. Their complete source bodies, decorators, assertions, parameterization, local definitions and comments match the baseline. Every retained top-level definition also matches. The original module retains substantial tests and all shared helper implementations; there is no imported-test duplication or forwarding facade.

Independent comparison against `git show <base>:tests/unit/engine/test_processor.py` produced:

```text
baseline_source_bytes_match=True
full_top_level_definitions_byte_and_AST_match=51
remaining_top_level_executable_nodes_match=True
baseline_collection=226
after_collection=226
remaining_items=183
moved_items=43
manifest_locators_resolve=6
helper_identity_bindings=11
helper_globals_still_original=True
```

The implementation's recursive definition check additionally records `definitions=349`, `test_definitions=213`; its source/AST comparison and controls were reviewed. The reviewer checked every executable top-level node outside definitions/imports as well, so unchanged module state and setup are not inferred from function parity alone. The collected node-ID multiset is identical after replacing only the expected new-module prefix for the five named classes; no duplicate IDs appear. Assertion mutations and duplicate collection controls correctly disagree with the baseline.

Removed imports resolve to the same owners in the extracted module. Runtime checks show the 11 shared bindings are identical objects and imported helper functions still execute against the original module's globals. Both `elspeth` and `elspeth_lints` resolved inside the reviewed worktree. The implementation also checked the imports of 17 external consumer modules. All literal `patch(...)` targets and their multiplicities are unchanged:

```text
elspeth.engine.processor.DAGNavigator.from_traversal_context=1
elspeth.engine.processor.run_boundary_checks=8
elspeth.engine.processor.best_effort=1
elspeth.engine.scheduler_drain.MAX_WORK_QUEUE_ITERATIONS=2
elspeth.engine.processor.scrub_text_for_audit=1
```

The six parsed DAG-manifest locators resolve in the actual post-move collection, including the class locator. The manifest, its exact-set test, and signed engine allowlist are byte-identical to the base. The old shutdown-test location in `config/cicd/enforce_tier_model/engine.yaml` is historical human rationale, not an executable boundary `test_ref`. The full old-to-new selector mapping in [architecture.md](architecture.md) and [processor-slice.md](processor-slice.md) is sufficient for this slice. No compatibility alias or signature edit is warranted merely to preserve that historical prose. Operator signature clearance is not claimed.

Measured current source:

| File | Physical lines | SHA-256 |
|---|---:|---|
| `tests/unit/engine/test_processor.py` | 9731 | `63d339df6114f3934d97a70e5f6772945777870cd1eefc5ed31f9ee932686572` |
| `tests/unit/engine/test_processor_transform_execution.py` | 1871 | `1914bd575b9d85f2131781231e7700c59ef4cfee57f2298f9dd0f09beb3e6f75` |

The 9,731-line remainder is explicitly unfinished against the eventual 5,000-line target. It is a legitimate first cohesive slice, not completion of the wider backlog.

## Validation evidence assessed

These completed raw summaries and exit codes were read from the lane-private evidence; the durable reproduction details are in [processor-slice.md](processor-slice.md).

| Run | Completed summary | Recorded exit |
|---|---|---:|
| Original processor suite | `226 passed in 13.01s` | 0 |
| Both resulting processor modules | `226 passed in 12.82s` | 0 |
| Selected whole-tree gates | `19 passed in 71.02s` | 0 |
| Mock-discipline repeat after inventory script addition | `3 passed in 10.94s` | 0 |
| Final integration repeat after LF emitter change | `3 passed in 20.51s` | 0 |

The 19 selected checks comprise mock discipline, all 11 file-walker checks, and five masquerade live-scan/control checks; they are not a claim that every masquerade unit test or every repository test ran. Adding the inventory Python script changed the mock scan's inputs, so its three checks were repeated. The file-walker pinned roots (`elspeth-lints/src`, `scripts/cicd`, `tests`) and masquerade roots (`src/elspeth`, `tests`, `scripts`, `elspeth-lints/src`) exclude that docs addition.

The root integration lane repeated the same three mock checks after the CSV emitter's LF options changed. Its complete canonical Python-source captures compare equal before/after; the reviewer read `integration-mock.log` and `.exit`, compared `integration-before.json` with `integration-after.json`, and recomputed their recorded digest. These artifacts are in `.claude/lanes/oversized-files-20261009/`:

```text
HEAD=9e481bc85a32ee15f808c5e2b16cab7634e57b8e
source_files=3837
before_after_equal=True
source_inventory_sha256=a12483571c3b8a30aab753039bceaae3be22404303a3f4611a7d1b4c1a03f9d8
```

The reviewer did not run a full suite, PostgreSQL testcontainers, or signed trust-tier verification. The change is a test-only class move plus analysis tooling/documentation; no production runtime, persistence, locking, plugin, or contract code changed. Future analyzer or production extractions require the broader gates stated in the architecture proposal.

## Inventory and architecture assessment

The script enumerates exact Git tree entries and reads raw blobs, rather than estimating line counts by regex or filesystem discovery. Its physical-line rule, strict thresholds, UTF-8/NUL exclusions, symlink handling and provisional category boundaries are explicit. The reviewer reran the positive/negative and mutation controls successfully, then reconstructed every fixed snapshot from the main CSV and complete row deltas and compared every row against fresh Git blob measurements:

```text
main: exact_row_equality=True entries=6133 gt5000=36 gt10000=13
published-pr281: exact_row_equality=True entries=6393 gt5000=38 gt10000=13
streaming-local: exact_row_equality=True entries=6135 gt5000=36 gt10000=13
repair-integrated: exact_row_equality=True entries=6395 gt5000=38 gt10000=13
```

The retained baseline is packaged as [inventory.csv.gz](inventory.csv.gz) to satisfy the existing 1,000 KB file-size hook. The final CSV serialization uses LF: independent AST comparison proves the generator change is exactly three `csv.DictWriter` keyword additions, `lineterminator="\n"`, with no other executable change. All three artifacts equal the previously reviewed bytes after CRLF-to-LF normalization, and their complete parsed row lists remain identical: 6,133 baseline rows and 1,726 rows in each delta/overlap file. No fixed snapshot or moving overlay was resampled for this normalization.

Independent decompression reproduces the final 1,091,258-byte CSV exactly; the gzip is 256,423 bytes, has a zero timestamp and no embedded filename. The final CSV SHA-256 is `ee7034f10dafba31ec134891e0a1b864703ab29eb197ce6253dbe11f3cee73c9`. The updated instructions correctly distinguish counting, serialization, lossless packaging and reconstruction. The root lane repeated the mock gate after the generator changed; the final evidence is recorded above.

The moving overlay remains a best-effort observation; matching rereads do not make it an atomic frozen tree. The report states this correctly. Smaller-file category labels are provisional, and excluded paths remain present in the inventory. Curated policy/config and executable test/fixture tooling are distinguished from generated artifacts; no category exclusion is presented as a completed refactor.

All 25 main-snapshot authored targets above 5,000 lines, including the nine above 10,000, are named in the architecture map; the streaming-added execution-service target is also covered. Proposed seams preserve real ownership, fixture scopes, private patch lookups, authority identities, complete scanner records, transaction boundaries and provider invariants. The plan requires measured collection and refusal controls, explicit stop/rollback conditions, dependency-ordered gate extraction and full-suite/PostgreSQL checks where effects broaden. Existing pins are not permission to weaken checks or derive expected data from the scanner under test.

The reserved-gate section clearly labels immutable source/pin characterization as static evidence rather than executed scanner parity. Its future work remains blocked on streaming integration or explicit bounded ownership release, followed by remeasurement. The review approval applies to this design and the completed first slice; those remaining implementations and behavioral proofs are still outstanding.
