# Lint CLI parser extraction

The lint argument grammar now has one authority,
`elspeth-lints/src/elspeth_lints/core/cli_parser.py`. Original `cli.py` retains
`main`, all command dispatch, finding computation, allowlist mutation,
authorization, signing/rekey, recovery and policy behavior. The two source files
are frozen on base `d38890ddcc5c6e3d570af645d5e3a25efddfea2a`; both are below
5,000 measured physical lines.

| Path | Physical lines | SHA-256 |
|---|---:|---|
| `elspeth-lints/src/elspeth_lints/core/cli.py` | 4951 | `9187db72b0003f1abe3efbba98530bcebc15bd932a43d2d6ce5f9654eb59db19` |
| `elspeth-lints/src/elspeth_lints/core/cli_parser.py` | 1206 | `11a61f1acd03f457de5d66ef597baae40248de07d66629b82bd903f9ced593a6` |

The original capture is 6,110 lines and 266,232 bytes, SHA-256
`24e214a3ba773412db2785bcf36c377eecb4434932617cdafd074fc078be991a`.
Regeneration assigns complete AST-owned functions/constants and their comments,
then adds the necessary explicit imports. Every one of 124 recursive definitions
retains exact full-source span and location-free AST equality. The 23 original
non-import top-level executable nodes partition into 16 exact retained nodes and
seven exact moved constant assignments, preserving their respective order.
Suppression COMMENT-token counters are equal; no suppression or forwarding
function is introduced. Final Ruff check, format check and git diff check exit 0.

## Binding ownership and bounded release

The parser owns `_build_parser`, the two shared judge-argument installers,
`_non_empty_string`, `_positive_int`, `_timezone_aware_datetime_arg`,
`_bounded_rationale_string`, `_sign_bundle_lanes` and their seven grammar
constants. CLI explicitly imports all fifteen names with honest same-name
reexports; unused original public imports `is_substantive_audit_anchor` and
`TRANSPORT_OPENROUTER` also remain available. All 49 original module import
bindings are preserved. There are no proxy functions, reverse imports, dynamic
module synchronization or test changes.

Function global ownership intentionally moves to `cli_parser`. Assigning a new
value to an old CLI private attribute does not rewrite the new owner globals.
The transport dictionary remains a single shared mutable object and retained
handlers keep their original lookups. The retained signing-env-file name set
still resolves both imported override-name constants; their values do not change.
No signing-key material or env-file contents were read, and no real handler,
signing, rekey or judge action was executed by this implementation lane.

Static preparation scanned 3,985 Git-tracked Python sources with zero parse
failures and recorded 25 CLI consumers. Existing moved-symbol direct-import APIs
remain available; no moved-name module-alias attribute read or literal patch
was found under controlled exact/near-match instruments. The independent
architecture alias inventory found 31 actual patch calls on ten retained names;
all remain original-owned. Observed private seams are preserved; arbitrary
external reflection is not asserted to have been resolved by those static scans.

Neither candidate is a literal member of the canonical 538 streaming paths.
Root obtained the owner's explicit release solely for lint argument grammar and
its existing focused tests. Dispatch, findings, signing/rekey, policy, the main
application CLI and other reserved surfaces remain outside that release. No
fixture, focused test, conftest, policy or signed YAML changes are part of this
slice. Eighty unchanged consumer/conftest/policy input hashes were captured before
application. Retained function and top-level-state byte parity bounds the change
within the released responsibility.

## Validation evidence

Root owns serial pytest and gate capacity, with streaming acceptance taking
priority. The implementation lane ran no pytest. Completed fresh baseline logs
and explicit exits were read from its private repository lane:

```text
baseline focused execution: 1033 passed, 1 existing skip in 20.74s; exit=0
baseline collection: 1034 tests collected in 2.42s; exit=0
actual parser-only baseline: 17 commands, 82 cases; exit=0
keyless all-rules baseline: 3163 complete finding records; exit=1
baseline all-rules stdout SHA-256:
  5f9945b0deac4e52f37fb3dbdb5bea3dea003aad1fb3a44e79a69ea89f3ac067
explicit boundary-rules baseline: []; exit=0
```

The skip is the existing roundtrip owner-empty case, already refused upstream
by argparse. Parser capture uses the actual production `_build_parser`, never
`main`, with exact root and all subcommand help, minimal/default Namespace values,
missing/choice refusals and fixed owner/rationale/integer/timestamp/lane/recovery
controls. It records exact stdout, stderr, exit and typed values. Paths are only
constructed; signing flags and env-file argument names are not executed or read.

Six actual private source-verifier subprocess controls exit 1 for intended
reasons: altered converter and grammar default, helper deletion, helper duplication,
constant deletion and constant duplication. Source/AST drift, duplicate definition
and executable-state drift are explicitly verified in each full error log.
`compare_parser.py` requires actual input-byte equality and uses deterministic
JSON serialization for type-sensitive dictionary controls. This prevents Python
equality from accepting integer 11 as float 11.0 or boolean False as integer 0.
Root identified that instrument gap before post-capture comparison; it is corrected
and explicitly controlled. Nine actual corrupted-capture subprocesses exit 1 with
`parser capture byte drift`: help/error byte, converter value, integer-to-float,
boolean-to-integer, default, dropped case, dropped command and dropped option.
An unchanged baseline capture is the known positive; its pretty reserialization
is also byte-exact, so formatting drift cannot explain the negative controls. `verify_runtime.py` checks
canonical aliases, new function globals, original public import identity,
transport dictionary identity, actual argparse converter objects, preserved
signing env-name set and unchanged input hashes without invoking handlers.

Completed post-change validation, with actual process exits read:

```text
after focused execution: 1033 passed, 1 same existing skip in 20.46s; exit=0
after collection: 1034 tests collected in 0.63s; exit=0
full collected node-ID multiset: exact equality, no normalization; verifier exit=0
actual parser capture: 17 commands, 82 cases; strict raw-byte comparison exit=0
parser captures: 178309 bytes each, exact equality
parser capture SHA-256:
  6170d8ed64f687e9f7303eccc53e78e50b8058f8c3ee317e5eccac10e86021fc
runtime identity probe: exit=0
scoped mypy: Success: no issues found in 2 source files; exit=0
```

All 1,034 actual baseline and after node IDs are preserved without module/path,
name, parameter or whitespace normalization. Complete-line capture requires the
count to agree with pytest's summary and rejects summary lines while accepting
parameter IDs containing spaces. Three actual corrupted-collection processes
reject duplicate, missing and altered parameter IDs with exit 1 for `collection
drift`; their fake summary counts are updated, so capture mismatch is not the
reason they fail.

The runtime probe confirms all fifteen CLI bindings are the new parser's actual
objects, all eight moved functions have its module name/globals, all 49 original
public import bindings preserve identity, and all seventeen actual argparse type
bindings refer to canonical parser helpers. Main retains original globals and
its canonical parser binding; transport-dictionary identity and signing env-name
set are preserved. All eighty captured unchanged input hashes match. Both
production package roots resolve inside the dedicated worktree. Runtime probing
built parsers and inspected import identity; it did not dispatch handlers.

The completed keyless all-rules comparison preserves all 3,163 records and
raw stdout/stderr bytes, with exit 1 on both sides and stdout SHA-256
`5f9945b0deac4e52f37fb3dbdb5bea3dea003aad1fb3a44e79a69ea89f3ac067`.
The explicit boundary rules return `[]` and exit 0 on both sides; raw stdout and
stderr bytes also match, stdout SHA-256
`37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570`.
`corpus-parity.json` records these actual full-byte comparisons. Keyless shape
verification is not operator signature clearance; the standing all-rules corpus
is preserved rather than treated as a zero-finding gate.

Final affected whole-tree validation completed with the same two inherited
failures:

```text
20 selected whole-tree checks: 2 failed, 18 passed in 110.72s; exit=1
root final Ruff check / format --check / git diff --check: exit=0
root controlled final comparison: exit=0
canonical Python sources: 3986 files
before/after full dictionaries, including HEAD d38890ddc: identical
inventory SHA-256:
  b9e9f104ead599b5b67a848fcbf06dfb5617418aa0b27c87254800bf59fc11fb
```

The failures are
`tests/unit/test_mock_discipline_baseline.py::test_no_unspecced_direct_mock_constructors`
(three unspecced constructors in protected
`tests/unit/web/execution/test_recovery_coordinator.py`, at 121:18, 368:18 and
421:18) and
`tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`
(eleven findings involving the protected mutation-fencing suite and its baseline).
Root's controlled comparison proves the complete eleven finding tuples and full
three-call mock diagnostic match the rebased baseline and clean published
`9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`. No findings were added by this slice;
no gate, baseline or protected file was changed to clear them. The selected set
is mock discipline, all canonical walker-authority checks, five established
masquerade scan/refusal controls and the no-hasattr gate.

`cli-parser/final-comparison.json` and its explicit exit in the private repository
lane preserve the frozen-tree, corpus and inherited-failure comparison and
mutation controls. The final before/after dictionaries and raw logs were read
independently by this implementation lane. These checks are bounded validation,
not a fully green integration claim. The production entry-infrastructure full
premerge gate remains pending, scheduled by root after streaming's priority work
and capacity release. No full-suite or operator signature clearance is claimed.

## Reproduction and private evidence

Run only when root has the serial capacity slot, from the dedicated worktree:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
export PYTHONDONTWRITEBYTECODE=1
COLUMNS=80 .venv/bin/python "$PRIVATE_EVIDENCE/capture_parser.py" "$LANE/parser-after.json"
.venv/bin/python "$PRIVATE_EVIDENCE/compare_parser.py" \
  "$LANE/parser-baseline.json" "$LANE/parser-after.json" "$PRIVATE_EVIDENCE/output-parity.json"
.venv/bin/python "$PRIVATE_EVIDENCE/verify_runtime.py" "$PRIVATE_EVIDENCE" "$PWD"
.venv/bin/python -m ruff check --no-cache \
  elspeth-lints/src/elspeth_lints/core/cli.py elspeth-lints/src/elspeth_lints/core/cli_parser.py
.venv/bin/python -m ruff format --no-cache --check \
  elspeth-lints/src/elspeth_lints/core/cli.py elspeth-lints/src/elspeth_lints/core/cli_parser.py
```

`PRIVATE_EVIDENCE` is this lane's `cli-parser/` workspace directory; `LANE` is
`.claude/lanes/oversized-files-20261009/cli-parser/`. These variable names are
placeholders for private local paths, not tracked absolute user-home references.
The exact focused selection is:

```bash
.venv/bin/python -m pytest -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/elspeth_lints/test_core_smoke.py \
  tests/unit/elspeth_lints/test_trust_tier_dump_edges.py \
  tests/unit/elspeth_lints/test_rotate_tier_model.py \
  tests/unit/elspeth_lints/test_rotate_exclude_judge_gated.py \
  tests/unit/elspeth_lints/test_justify.py \
  tests/unit/elspeth_lints/test_reaudit.py \
  tests/unit/elspeth_lints/test_reaudit_multi_rule.py \
  tests/unit/elspeth_lints/test_reaudit_sidecar.py \
  tests/unit/elspeth_lints/test_judge_tools.py \
  tests/unit/elspeth_lints/test_judge_signature_diagnosis.py \
  tests/unit/elspeth_lints/test_cli_sign_bundle.py \
  tests/unit/elspeth_lints/test_cli_rekey.py \
  tests/unit/elspeth_lints/test_cli_migrate_judge_scope.py \
  tests/unit/elspeth_lints/test_judge_coverage.py \
  tests/unit/elspeth_lints/test_per_file_blanket_ratchet.py \
  tests/unit/elspeth_lints/test_judge_quality.py \
  tests/unit/elspeth_lints/test_trust_boundary_diff.py \
  tests/unit/elspeth_lints/test_override_rate.py \
  tests/unit/elspeth_lints/test_allowlist_yaml_roundtrip_contract.py \
  tests/unit/elspeth_lints/test_judge_mcp_server.py \
  tests/unit/elspeth_lints/test_source_excerpt_security.py \
  tests/unit/elspeth_lints/test_pre_commit_triggers.py
```

Redirect to a distinct log and record its actual exit; collection adds
`--collect-only -q`. Keyless before/after all-rules and explicit boundary rules
must compare complete diagnostics and exits, with frozen source snapshots.

Private artifacts include `original-cli.py`, `definition-fingerprints.json`,
`consumer-inventory.json`, `preparation.json`, `candidate-source-parity.json`,
`runtime-parity.json`, `corpus-parity.json`, `baseline-ids.json`, `after-ids.json`, `collection-parity.json`,
`collection-negative-controls.json`,
`unchanged-inputs.json`, `source-negative-controls.json`,
`output-negative-controls.json`, `output-instrument-controls.json` and individual negative
process logs/exits. Private scripts remain outside the scanned repository and
are not staged. No commit, push, merge, full-suite, PostgreSQL, CI or operator
signature-clearance claim is made by this lane. This production enforcement-entry
change still requires the parent-scheduled full premerge gate after streaming's
priority work and capacity release.
