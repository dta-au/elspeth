# Independent review of the lint CLI parser extraction

**Approved for the bounded extraction, with no introduced findings.** This is not full premerge clearance: the canonical production premerge gate remains pending, and the two already-characterized streaming-base integration failures remain unresolved.

Reviewed base: `d38890ddcc5c6e3d570af645d5e3a25efddfea2a`, on published streaming `9f26833c`. The streaming owner explicitly released this narrow parser extraction and its existing focused tests. The release does not cover unrelated CLI/runtime changes. Final working-tree status contained only the retained lint CLI and new parser module.

## Source and ownership

Independent complete-file source/AST comparison preserves all 124 recursive definitions exactly: 116 remain in `cli.py`, and eight move to `cli_parser.py`. This includes `main`, every handler, nested definitions, signatures, decorators, assertions, help/error strings and converter bodies. All 23 non-import executable module nodes are preserved exactly, partitioned into 16 retained and seven moved assignments. Assertion-change, missing-definition, duplicate-definition and duplicate-state controls reject mutations.

| Source | Lines | SHA-256 |
|---|---:|---|
| `elspeth-lints/src/elspeth_lints/core/cli.py` | 4,951 | `9187db72b0003f1abe3efbba98530bcebc15bd932a43d2d6ce5f9654eb59db19` |
| `elspeth-lints/src/elspeth_lints/core/cli_parser.py` | 1,206 | `11a61f1acd03f457de5d66ef597baae40248de07d66629b82bd903f9ced593a6` |

The retained file is a substantive command implementation, not a forwarding file. Dispatch order, finding computation, allowlist writes, signing/rekey handlers and entry-point behavior remain source-identical. The new module imports only its parser dependencies and does not construct a parser at import time. A fresh-process import of `cli_parser` did not import `cli`, confirming the intended one-way dependency.

All 15 moved function/constant names are explicitly imported back as their canonical objects. Root confirmed this as an intentional scope decision to preserve named imports, including names with no current repository consumer. Independent runtime checks establish identity for all 15 bindings. All eight moved functions use the parser module's globals; all 101 retained top-level functions use the original CLI globals. The ten observed private patch-target names remain bound in the original module, and the affected existing tests pass.

This is an intentional global-ownership transfer. Rebinding a moved converter or scalar constant on `cli` does **not** rewrite the moved function's `cli_parser` globals. The review does not claim compatibility for hypothetical external private monkeypatches. No proxy, wrapper, reverse import or dynamic globals synchronization was introduced. The transport dictionary is the same shared object, and retained handlers still resolve their dependencies in `cli`.

## Parser and focused execution

I inspected the parser-only capture instrument: it calls `_build_parser().parse_args(...)`, never `main` or a command handler. Before/after capture files are exactly equal as raw bytes:

```text
commands: 17
cases: 82
capture size: 178,309 bytes
capture SHA-256: 6170d8ed64f687e9f7303eccc53e78e50b8058f8c3ee317e5eccac10e86021fc
parser-validation.exit: 0
```

This preserves captured command/action ordering, defaults, choices, argument metadata, root/subcommand help, error output, exit status and serialized namespace values. The comparison uses raw bytes, not ordinary Python dictionary equality. I independently verified that integer-to-float and boolean-to-integer mutations are rejected by type-sensitive serialization even though ordinary dictionary equality accepts them. The private comparator also has nine rejected mutation captures covering help/error bytes, converter results, defaults and missing cases/commands/options.

The 22-module focused selection retains all 1,034 complete node IDs, independently compared as full multisets without normalization. Completed results read from `.claude/lanes/oversized-files-20261009/cli-parser/`:

```text
baseline.exit: 0; 1,033 passed, 1 skipped in 20.74s
after.exit: 0; 1,033 passed, 1 skipped in 20.46s
baseline/after collection exits: 0; 1,034 IDs each
mypy.exit: 0; no issues in the two source files
```

The skip is unchanged: the empty-owner roundtrip case is already refused by argparse upstream. It is not a new exclusion. Parser characterization did not execute live signing, rekeying, provider calls or allowlist operations. Root owned the coordinated test slot; this reviewer ran no pytest or lint scans.

## Diagnostic and gate parity

Independent raw-byte comparisons of normal CLI entry-point output establish:

```text
keyless all-rules baseline/after exits: 1 / 1
finding records: 3,163 / 3,163
stdout: exactly equal, 1,304,380 bytes
stdout SHA-256: 5f9945b0deac4e52f37fb3dbdb5bea3dea003aad1fb3a44e79a69ea89f3ac067
stderr: exactly equal, 57,766 bytes
explicit boundary baseline/after exits: 0 / 0
boundary findings: [] / []; stdout and 1,461-byte stderr exactly equal
```

The boundary selection is `trust_boundary.tests,trust_boundary.scope,trust_boundary.tier`. These are keyless results, not operator signature clearance. The existing finding corpus was preserved, not cleared or suppressed.

The 20 applicable whole-tree checks completed with exit 1: **2 failed, 18 passed in 110.72s**. The failing nodes remain `test_no_unspecced_direct_mock_constructors` and `test_live_tree_has_zero_unbaselined_findings`. I independently parsed all 11 complete masquerade tuples and the full three-mock diagnostic and compared them with both the prior rebase run and isolated untouched streaming-base reproduction. They match exactly; no new finding was introduced.

The entire before/after canonical Python input dictionaries are identical, including HEAD. I independently recomputed:

```text
canonical Python files: 3,986
input-dictionary SHA-256: b9e9f104ead599b5b67a848fcbf06dfb5617418aa0b27c87254800bf59fc11fb
```

Both reviewed source hashes still match after validation.

## Remaining limits

No severity-ranked finding was introduced by this slice. The bounded evidence supports the extraction and a local reviewed commit. The canonical full production premerge gate is still pending and must be scheduled with streaming priority respected. This report does not certify that gate, operator signatures, PostgreSQL behavior, unrelated owner repairs, or resolution of the inherited red integration checks.
