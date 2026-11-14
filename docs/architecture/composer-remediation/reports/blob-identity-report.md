# Canonical blob selector implementation — Task 1

Status: Task 1 implemented and frozen for independent Astra review. Bounded
behavior, real PostgreSQL, mutation and static verification is recorded below.
Parent-owned combined merge gates and review remain unclaimed.

Base: `1bc91710fbbdd205376f71dadf8aa3ca557229d8`.
Exclusive worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-resource-identity`.
Scope: Task 1 in `resource-identity-plan.md`, including the four accepted
`resource-identity-design-review.md` clarifications. Task 2 parked-graph
normalization remains untouched and held out.

## Behavior and evidence boundaries

The new `web/resource_identity.py::canonical_resource_uuid(value: str)` returns
the canonical lowercase hyphenated UUID or `None` on `ValueError`; it does not
strip whitespace. Validated external selectors flow through tool metadata,
inline wiring, mutation admission/execution, source binding and inspection,
content readers, planner information identities, and conservative proposal
reference projection. Successful content/attachment echoes use the returned
owned record identity; successful deletion echoes the canonical selector.

The two added planner validation arms use the actual existing metadata and
inspection argument models. Invalid shapes, extra fields, malformed UUIDs and
whitespace UUIDs create no information identity. Blob identity alone is
normalized; offset and expected-content-hash discriminators remain intact.

`contracts/blobs.py::names_same_blob`, embedded marker grammar and strict
proposal sentinel validation are unchanged. Conservative marker projection
can retain a resource whose braced/hyphenless graph marker the production
inline resolver rejects. Nonparseable nonempty references remain references.
Plural source binding rejects equivalent selector duplicates before lookup or
mutation and derives bindings from returned metadata.

D1 admission, fenced read custody, recovery observation, typed final
publication-race refusal and exact accepting-proposal exclusion are retained.
No schema, public service signature, receipt format, private argument hash,
owned row identity, snapshot or stored proposal dictionary was normalized.
The receipt writer and `_validated_blob_effect_receipt` reader were inspected
and left unchanged. Real acceptance/retry tests exercise both tools and the
receipt reader, including exact-document selector-spelling tamper rejection.

Compatibility: existing canonical pending/applied proposals and receipts stay
readable without rewriting. Formerly noncanonical successful `blob_id`
response values intentionally become canonical. Blob whitespace remains
invalid. This work does not close umbrella R2/XC-13 findings.

## TDD record

All commands ran in the explicit worktree above, through its linked
`.venv/bin/python`. `PYTHONPATH` was explicitly
`<worktree>/src:<worktree>/elspeth-lints/src`. Import provenance printed:

```
/workspace/elspeth/.claude/worktrees/composer-remediation-resource-identity/src/elspeth/__init__.py
/workspace/elspeth/.claude/worktrees/composer-remediation-resource-identity/elspeth-lints/src/elspeth_lints/__init__.py
```

No tests started until the parent granted the focused runner slot. No
production code was written before the measured red cohorts.

| Capture | Actual exit/result | Interpretation |
| --- | --- | --- |
| `blob-identity/red1.log` | 1; 144 failed, 127 passed; 32.70s | Reproduced raw lookup, echo, guard, planner and proposal identity defects. Also exposed three fixture-shape mistakes, explicitly corrected below. |
| `blob-identity/red2.log` | 1; 55 failed, 35 passed, 74 deselected; 38.01s | Corrected fixture shapes; canonical controls pass. Alias metadata/source lookups, read/delete echoes, bound admission and receipt publication fail at intended identity consequences. |
| `blob-identity/red3.log` | 1; 1 failed, 4 passed, 169 deselected; 9.63s | Real scripted planner alias metadata lookup fails; original D1 bound test initially lacked a guard-specific reason assertion. |
| `blob-identity/red4.log` | 1; 4 failed, 169 deselected; 8.41s | Both dialects and both mutation tools refuse at late resource publication rather than the required bound-composition admission guard. |
| `blob-identity/green1.log` | 1; 388 passed, 1 failed; 101.74s | All tool/source/projection/receipt cases pass. Sole failure was an incorrect new-test assumption that repeated discovery should produce `DISCOVERY_NO_GAIN`. |
| `blob-identity/green2.json` | 0; 97 passed, 77 deselected; 85.51s pytest; frozen=true | Corrected actual planner manifest closure oracle plus D1 spelling/dialect admission and typed publication-race cohort. |
| `blob-identity/ruff.json` | 0; frozen=true | All 13 then-touched production/test files. |
| `blob-identity/mypy.json` | 0; 5 production files; frozen=true | Existing unused-config notes only. |
| `blob-identity/contracts.json` | 0; frozen=true | Contract alignment and soft-mapping census unchanged. |
| `blob-identity/affected.json` | 0; 950 passed, 1 warning; 369.34s pytest; frozen=true | Complete affected blob tools, inline markers, planner, plural binding, reference projection and receipt modules. Warning is Pydantic's pre-existing `ChatCompletionReasoningItem.summary` ReadOnly notice. |
| `blob-identity/postgres.json` | 0; 31 passed; 31.61s pytest; frozen=true | Real PostgreSQL bound aliases, writer-debt admission custody and live-deletion reader/mutator custody. |
| `blob-identity/negative-controls.json` | 0 orchestrator; all eight mutant subprocesses exit 1; frozen=true | Deliberately restored defects detected with canonical positives retained; see below. |
| `blob-identity/ruff-final-verified.json` | 0; all 15 then-touched files; frozen=true | Includes PostgreSQL tests and subsequent isolated test-strengthening edits. |
| `blob-identity/gates.json` | 1; 1083 passed, 3 skipped, 1 xfailed, 2 failed; 945.19s pytest; frozen=true | Both exact failures also reproduce on the base; five narrow locator repairs followed. No deferred Landscape CI proof was selected. |
| `blob-identity/authority-base.log` | 1; same two tests fail on exact base; 11.18s | Baseline reproduction with both import roots measured inside the git-archive copy. |
| `blob-identity/authority-repair-final.json` | 0; 6 passed; 9.27s pytest; frozen=true | Exact two failing gates plus their DML/escape refusal controls, on final formatted locator repair. |
| `blob-identity/inspections.json` | 0; 171 passed; 6.99s pytest; frozen=true | Existing real inspect-source tool and source-inspection regression coverage. |
| `blob-identity/final-static.json`, `final-format.json` | 0; 16 files; frozen=true | Final changed production/tests including the key-free locator repair. |

Fixture corrections were test-only: `inspect_source` returns its ID under
`redacted_identity`, the inline target syntax is
`node:classify.options.prompt_template`, and singular CSV binding needs the
existing observed schema option. These failures were not counted as missing
production behavior.

Ruling: current planner production behavior intentionally permits discovery
rereads; there is no active no-gain refusal branch. The scripted test now
starts with a canonical unresolved metadata fact and checks that alias
metadata success causes the actual next provider request to close that fact.
No discovery policy was changed. The cost if wrong would be a missed planner
fact-identity regression, addressed additionally by a planner-only mutant.

Exact early commands (each with the explicit environment prefix above and
`> blob-identity/<capture>.log 2>&1`, checking the real process exit):

```
python -m pytest tests/unit/web/composer/test_blob_resource_identity.py tests/unit/web/composer/test_blob_planner_identity.py tests/unit/web/sessions/test_proposal_blob_refs.py tests/unit/web/composer/test_set_source_from_blobs.py -n 0 -o pythonpath=<worktree> --basetemp=<worktree>/.claude/lanes/composer-remediation-wave1/blob-identity/red1-tmp
python -m pytest tests/unit/web/composer/test_blob_resource_identity.py tests/unit/web/sessions/test_blob_proposal_accept.py -k 'canonical or braced' -n 0 -o 'pythonpath=src elspeth-lints/src .' --basetemp=<worktree>/.claude/lanes/composer-remediation-wave1/blob-identity/red2-tmp
python -m pytest tests/unit/web/composer/test_pipeline_planner.py::test_alias_metadata_success_resolves_the_canonical_fact_in_the_next_planner_request tests/integration/web/composer/test_freeform_proposal_prevalidation.py -k 'alias_metadata_success or (blob_admission_refuses_known_ineligible and bound and braced)' -n 0 -o 'pythonpath=src elspeth-lints/src .' --basetemp=<worktree>/.claude/lanes/composer-remediation-wave1/blob-identity/red3-tmp
python -m pytest tests/integration/web/composer/test_freeform_proposal_prevalidation.py -k 'blob_admission_refuses_known_ineligible and bound and braced' -n 0 -o 'pythonpath=src elspeth-lints/src .' --basetemp=<worktree>/.claude/lanes/composer-remediation-wave1/blob-identity/red4-tmp
python -m pytest tests/unit/web/composer/test_blob_resource_identity.py tests/unit/web/composer/test_blob_planner_identity.py tests/unit/web/sessions/test_proposal_blob_refs.py tests/unit/web/composer/test_set_source_from_blobs.py tests/unit/web/sessions/test_blob_proposal_accept.py tests/unit/web/composer/test_pipeline_planner.py::test_alias_metadata_success_resolves_the_canonical_fact_in_the_next_planner_request -n 0 -o 'pythonpath=src elspeth-lints/src .' --basetemp=<worktree>/.claude/lanes/composer-remediation-wave1/blob-identity/green1-tmp
```

`<worktree>` is the exact absolute worktree path stated above, not an
environment-dependent checkout. Red1's pytest `pythonpath` override names the
repository root only; the explicit exported `PYTHONPATH` still contained both
source roots and both import paths were measured before that run. Later
commands supply both roots in both mechanisms. Later exact commands, exits,
elapsed time, cwd, environment, HEAD and complete before/after file hashes
are in each `blob-identity/*.json` capture produced by `run_frozen.py`.

## Keyless lint corpus

Before and after commands:

```
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing python -m elspeth_lints.core.cli check --rules all --root src/elspeth
```

Both return 1. The before scan completed on unchanged base production before
the first production edit. The after capture is `lints-after.json`, with
frozen=true. Full logs are `lints-before.log` and `lints-after.log`.

The controlled comparator parses every diagnostic into
`(path, rule, message)` and uses `Counter` multiplicity. It removes diagnostic
line/column offsets and only embedded ` at <path>:<line>` offsets; it preserves
the complete remaining message and every occurrence. Positive fixture parsing
and a nonfinding warning negative control both passed. A completeness check
also found zero unparsed structured diagnostics and zero other nonwarning
lines in all three captured lint logs (`lint-parser-completeness.json`). `lint-delta.json`
records 5433 before occurrences and 5436 after, zero removed and exactly:

| Path | Rule | Complete message | Added multiplicity |
| --- | --- | --- | --- |
| `web/composer/pipeline_planner.py` | R6 | `Exception swallowed without re-raise or explicit error: except PydanticValidationError:` | 2 |
| `web/resource_identity.py` | R6 | `Exception swallowed without re-raise or explicit error: except ValueError:` | 1 |

The added diagnostic locations in the after capture are planner `446:8` and
`455:8`, and shared parser `10:4`. The other two same-message planner arms at
`464:8` and `473:8` were already present in the before corpus.

Author rationale: the shared parser's required invalid-selector contract is
`None`; the planner's invalid-model contract is no fact `()`, matching its
existing content/attachment arms. These are deliberate parse rejections,
not ignored operation failures. Raw exit 1 remains reported; independent
Astra review must adjudicate these flows. No suppression, signed-allowlist or
signature edit, signed clearance claim or operator-approval blocker was introduced.

The explicit companion `trust_boundary.tests,trust_boundary.scope,trust_boundary.tier`
command returns 1 with 205 `R_TB_TESTS_FINGERPRINT_MISMATCH` occurrences (204
distinct complete messages). Excluding informational `R_TB_SUPPRESSED` rows
produced only by the all-rules command, its full-message Counter is identical
to the base capture: zero added and zero removed. See `trust-boundary.json`,
`trust-boundary.log`, and `trust-boundary-delta.json`. No fingerprint metadata
was edited.

The all-rules and companion captures precede the five later test-data locator
repairs; their complete input snapshots are not presented as final-candidate
snapshots. Production is byte-identical across those captures and the final
source. A controlled AST inventory of all production call keyword `test_ref`
values found 205 literal references and zero references to the edited
architecture test file. The known YAML importer binding, a synthetic binding
to the changed target and an unbound-string negative control all worked.
The sole nonliteral site is `contracts/trust_boundary.py:234`, forwarding
`test_ref` into `TrustBoundaryMetadata`; that factory was inspected and does
not construct a new target. Thus no bound honesty-test body is affected by
the locator edits. Evidence: `locator-boundary-scope.json`.

## Narrow authority locator repair

The terminal 1089-test gate run reported exactly these failures:

```
tests/unit/architecture/test_session_db_mutation_authority.py::test_inline_csv_comparison_read_admission_is_exact
tests/unit/architecture/test_session_db_mutation_authority.py::test_live_connection_domain_classification_is_exact
```

Both fail on an exact `git archive` of base `1bc91710fbbdd205376f71dadf8aa3ca557229d8`
too (`authority-base.log`, `authority-base-test.json`, `authority-base-provenance.json`). The inline CSV
read's reviewed locator was already five lines stale on the base; this patch
adds three more lines before it. The classification failure is the previously
changed Landscape probe's stale expected line, not a new member or authority.

`authority-binding-comparison.json` contains every field of every member,
not merely the first pytest diff: 69 expected, 69 base and 69 candidate
classifications. Base and candidate have identical membership, multiplicity,
paths, qualified methods, tables/domains, operations, fingerprints, ordinals,
authorities, line locators and escape flags; both had the same one unexpected
and one stale expected row. Four relevant read functions also have identical
full function ASTs before/after, including their query and guard bodies.
Their exact source text and AST hashes are included in that artifact.

The comparator's equal-identity negative control and nine independent
positive controls changing path, symbol, table/domain, operation,
fingerprint, ordinal, authority, line, or escape flag all behaved correctly.
An initial ad-hoc static invocation omitted explicit source-root provenance
and was discarded, not used as proof; `invalid-authority-probe.json` preserves
that limitation. The accepted comparison explicitly sets both roots and
asserts both imported paths inside this worktree.

Ruling: after reviewing those measurements, the parent authorized only five
key-free locator updates in the existing Python test inventory. The canonical
derivation path is the gate's `scan_production_writers`, which emitted these
live line fields; no blanket regeneration was used. No identity member,
fingerprint, authority, policy, signature or escape flag changed. The cost if
this ruling were wrong would be an incorrect read admission; the unchanged
query/guard AST proof and the DML/escape refusal controls address that risk.

| Existing binding | Reviewed line | Base live line | Candidate live line / repaired value |
| --- | --- | --- | --- |
| `_sync_get_blob` | 501 | 501 | 502 |
| `_sync_list_blobs` | 536 | 536 | 538 |
| `_sync_list_ready_blob_inline_descriptors` | 561 | 561 | 563 |
| `_header_only_inline_csv_conflict` | 1826 | 1831 | 1834 |
| `probe_schema_shape` expected classification | 935 | 955 | 955 |

The first repair cohort passed six tests; an indentation-only format cleanup
was followed by the same six tests on final source: six passed, exit 0,
frozen=true. The final cohort includes the two exact gates plus injected CSV
DML/connection-escape negatives and general read-review escape negatives.
The whole 1089-test selection was not repeated after this isolated five-line
test-data repair. Its original exit 1, the skipped/xfail counts, the base red,
and final targeted green remain distinct evidence. The three skips require
Python 3.13 PEP 696 support; the recorded xfail is
`test_all_production_sessions_writers_are_reviewed_typed_authorities` (existing
Sessions authority inventory drift). No signed metadata was edited.

## Focused PostgreSQL and negative controls

The parent explicitly authorized the three focused PostgreSQL nodes after
the 950-test run. Docker server `29.9.2` was measured. The 31 cases consist of
12 bound-selector spellings/tool combinations, six admission waiters with
canonical/braced/URN arguments observing real competing writer debt, and 13
live-deletion contenders, including both content readers and mutation tools.
The public typed-UUID reader remains a single canonical control.

The 950-test evidence precedes PostgreSQL test additions. Production code is
unchanged between these runs. Two later isolated test strengthenings make the
nested binding a full inline marker and extend second-pending-reference
acceptance refusal from update to both update/delete; those are rerun in the
final gate capture rather than implying they were present in the earlier
950-test run.

Each mutant is a separate copy under `blob-identity/mutants/`, with both source
roots imported from that copy and explicit `PYTHONPATH`, pytest pythonpath,
`-n 0`, rootdir and lane-local basetemp. No main-tree source was mutated.
`negative_controls.executed.py` preserves the executed mutation/selection
rules. `negative-mutants.json` lists the exact deterministically generated
per-case argv, cwd, environment and observed exits/counts; full subprocess
output is preserved in `negative-<name>.log`. The outer frozen capture is
`negative-controls.json`.

| Mutant | Exit | Expected failures | Passing canonical controls | Observed consequence |
| --- | --- | --- | --- | --- |
| `postgres-guard-raw` | 1 | 2 | 2 | Braced bound update/delete preview incorrectly stageable on real PostgreSQL. |
| `metadata-raw` | 1 | 1 | 1 | Braced metadata selector returns missing instead of the owned blob. |
| `execution-guard-raw` | 1 | 2 | 2 | Real update/delete effects proceed despite a bound canonical live-state marker. |
| `admission-guard-raw` | 1 | 2 | 2 | Bound alias admission incorrectly returns stageable. |
| `content-echo-raw` | 1 | 2 | 2 | Both content readers echo the raw braced selector. |
| `planner-key-raw` | 1 | 21 | 4 | Aliases create distinct keys; real alias-success loop does not close the canonical manifest fact. |
| `proposal-projection-raw` | 1 | 36 | 6 | Alias reference projection/retention fails, including canonical accepting rows retained by a second alias proposal. |
| `receipt-normalized-hash` | 1 | 4 | 2 | Both tools violate exact raw hash binding and fail to reject equivalent-selector spelling tampering after effect creation. |

The final receipt mutant specifically changes only the receipt argument hash
helper to hash `{**arguments, "blob_id": canonical_blob_id}`. Its alias
recovery cases fail the raw hash oracle, and its canonical-to-uppercase stored
argument tamper cases fail because the expected `AuditIntegrityError` is not
raised. Canonical untampered recovery remains usable for both tools.

## Changed paths

Production (five files; two receipt modules inspected but unchanged):

```
src/elspeth/web/resource_identity.py                         NEW
src/elspeth/web/composer/tools/blobs.py
src/elspeth/web/composer/tools/sources.py
src/elspeth/web/composer/pipeline_planner.py
src/elspeth/web/sessions/proposal_blob_refs.py
```

Tests:

```
tests/fixtures/resource_identity.py                         NEW
tests/unit/web/composer/test_blob_resource_identity.py       NEW
tests/unit/web/composer/test_blob_planner_identity.py        NEW
tests/unit/web/composer/test_pipeline_planner.py
tests/unit/web/composer/test_set_source_from_blobs.py
tests/unit/web/sessions/test_proposal_blob_refs.py
tests/unit/web/sessions/test_blob_proposal_accept.py
tests/integration/web/composer/test_freeform_proposal_prevalidation.py
tests/testcontainer/web/test_blob_mutation_admission_postgres.py
tests/testcontainer/web/test_blob_read_custody_race.py
tests/unit/architecture/test_session_db_mutation_authority.py  FIVE LOCATORS ONLY
```

## Final source freeze and limits

The final base remains `1bc91710fbbdd205376f71dadf8aa3ca557229d8`. Exactly 16
files differ, including four new files and five production files. Full
before/after source snapshots accompany the frozen captures. The final
16-file hashes, exact paths, timestamp and patch hash are in
`blob-identity/source-freeze.json`; the five production files are verified
byte-identical across the planner/D1, 950-test affected, 31-case PostgreSQL,
all mutant, mypy, keyless lint, initial gate, inspection and final repair/static
captures. The final source has no subsequent edits.

The complete patch, including all four untracked new files, is
`blob-identity-review.diff` (75,606 bytes), SHA-256:

```
0b31812f5559ac9326ab7b13cadf8e020924d4309f05c523816ca1d53ccd90a3
```

The full patch dry-run applies to the exact base archive with exit 0;
`git diff --check HEAD` also exits 0. Final artifact/source consistency is
recorded in `blob-identity/handoff-verification.json`. The complete 69-member
expected/base/candidate classification snapshots are preserved in
`authority-binding-comparison.json` alongside the full controlled deltas.

Parent owns combined ordinary, long-proof and
full PostgreSQL gates and Astra review. No full suite, independent hours-long
proof suite, provider, install, Page, migration, signing, commit or push has
been performed in this lane.
