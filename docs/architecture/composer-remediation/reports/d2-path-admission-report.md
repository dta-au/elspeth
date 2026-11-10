# D2 configured-root path admission

Implementation is ready for the parent's independent review. No commit or
integration was performed. Worktree:
`/workspace/elspeth/.claude/worktrees/composer-remediation-d2`.
HEAD remains `f9c23c59c63bbd3099029e995a778bade594f6c8`.

Complete review patch: `d2-path-admission-review.diff`, including both new
untracked files. SHA-256:
`f0308cf72acfb23967069c712d3e3d1bde3f567e70d4ed91251485b52e831d39`.
The patch's reverse application check and whitespace check both exited 0.
Exact artifact information is in `d2-checks/review-artifact.json`.

## Result and ownership

`contracts/composer_paths.py` owns the frozen nominal
`McpConfiguredRootAuthority`. It canonicalizes an operator-supplied `Path`,
permits a missing directory, and rejects an existing non-directory. The type
was moved from the initially proposed `web/paths.py` after the contracts gate
identified its cross-package use; the parent explicitly authorized this
ownership correction. All direct imports now use its owning module. No
whitelist or suppression was added.

The shipped stdio builder issues the authority once. All eight registered MCP
path-writing tools receive it through `create_server`, `_dispatch_tool`,
`execute_tool`, and `ToolContext`. Existing source, sink, and provider guards
validate the entire proposed/merged options before effects. Both singular and
plural bulk source branches now enforce missing-authority refusal. The live
registry measurement is `d2-checks/live-writers.json` (exit 0), with all eight
nominated writers present and the authority's actual module recorded.

MCP paths resolve under the canonical configured root, including absolute
contained paths and contained symlinks. Escaping traversal, absolute paths,
and symlinks refuse. Containment does not test leaf existence. Existing bulk
sink spelling normalization remains, without a fabricated MCP session
segment. Malformed path values use argument/policy channels before `Path`;
supported provider `persist_directory=None` remains nullable during authoring
and both YAML materializations. Missing authority grants no local file writes
but permits non-file authoring. Wrong owned types and mixed Web custody are
internal contract errors, including the empty-validation shortcut.

The same authority reaches runtime validation, path policy, executable YAML,
compiled identity YAML, and the profiled identity caller. Cache settings scope
includes authority mode and canonical root, with existing settings/plugin
identity retained. The fake filesystem session token was removed. Preview
still uses the real bounded worker and plugin/graph checks. CLI root help and
route-specific repair guidance now describe the authoring boundary.

Web directory helpers and session resolution retain their tenancy semantics.
Legacy session loading preserves private state; escaped paths independently
fail the named preview `path_allowlist` check. Public export strips private
source/output/provider paths and does not replace private working state.

## Behavior evidence

The real registered SDK handlers and catalog cover all eight tools, with
separate bulk singular-source, plural-source, output, and provider branches.
Assertions include relative/absolute admission, outside/traversal/symlink
refusal, saved private bytes, whole private/public state, version/content hash,
empty affected nodes, exact component/code, and semantic-versus-argument audit
status. Non-path patches cannot reauthorize a legacy escaped path.

Additional controls cover early valid file sources followed by late bad
output/provider components in both bulk source forms; prepared inline-source
refusal with real SQLite/blob rows and filesystem bytes; missing root/leaf;
root and leaf symlinks; changed-symlink preview; malformed live and legacy
paths; nullable provider materialization; literal executable/identity paths;
the real profiled identity helper; canonical symlink-root deduplication versus
different roots and missing-authority mode; real Web session confinement;
public redaction and private-state preservation.

## Exact verification

All command arrays and raw exits are preserved in
`d2-checks/final-receipts.json` and
`d2-checks/post-contract-receipts.json`. Both worktree source roots were
exported in `PYTHONPATH`; pytest additionally received explicit
`-o pythonpath=<worktree>/src <worktree>/elspeth-lints/src` and `-n 0`.
Import provenance is in the runner logs. Final and subsequent runs use a
project-lane `--basetemp`; two initial recovery runs used pytest's default
temporary directory before this was corrected.

| Check | Actual result |
|---|---|
| Diagnostic functional/rejection/runtime cohort plus whole attribute and masquerade gates | exit 0; **1,576 passed, 3 skipped**, 782.86 s |
| Final contracts-placement cohort: real MCP handlers, paths, inline atomicity, authoring/materialization/identity, preflight, resolver, leaf and attribute controls | exit 0; **579 passed**, 69.84 s |
| Final scoped ruff and formatting | both exit 0 |
| Final scoped mypy | exit 0; 14 production files |
| Final contracts, including soft-mapping census | exit 0; no repin |
| Final generated skill inventory check | exit 0 |
| Final three boundary rules | raw exit 1; 205 findings, as in parent's clean-HEAD baseline |
| Verified location-only boundary comparison | exit 0; no semantic additions/removals or mapping/derivation errors |

The diagnostic 1,579-item cohort was frozen before the type-placement
correction. Its only skips are the three existing PEP 696 cases requiring
Python 3.13; the current interpreter is Python 3.12.14. After moving the type
and its imports, the final 579-item cohort and all relevant static checks ran
under a fresh freeze. One new late-provider test fixture also received a
distinct output prefix so its earlier valid component has no field collision.

Final before/after/review snapshots all match: **6,207 inputs**,
SHA-256 `60a4be8cb90a2b4cc89af42356f1cee054d739a132b1b9494f12dcaa784dde59`.
The freeze includes the complete tracked tree and untracked source/test
inputs, rejects symlink ancestors, opens regular files without following
links, and records other tracked link targets. The captured source/test
inventory contains no symlinks. Evidence:
`post-contract-before.json`, `post-contract-after.json`,
`post-contract-frozen.txt`, and `review-freeze.json`, all under `d2-checks/`.

The raw aggregate post-contract runner exited 1 because the raw boundary
scan and the deliberately strict diagnostic-ID comparison exited 1. This is
not a claim that every repository gate is green. The independent normalized
comparison below establishes that D2 adds no boundary finding.

## Boundary baseline and exact location-derived ID changes

Parent baseline: `d2-boundaries-baseline.json`, clean HEAD f9c23, raw exit 1.
Candidate: `d2-checks/post-contract-boundaries.json`, raw exit 1. Both scans
used `trust_boundary.tests,trust_boundary.scope,trust_boundary.tier` with JSON
output and the same source roots. The strict comparison is retained in
`d2-checks/boundary-delta.json`; the verified comparison is
`d2-checks/boundary-location-delta.json` (exit receipt 0).

The scanner's `shared.py:819` derives diagnostic IDs from
`rule_id|file_path|call.lineno|call.col_offset`. Every baseline and candidate
ID was independently checked against that formula. Every changed line maps
to identical HEAD source text. All other diagnostic fields, including the
resolved and recorded test fingerprints in each message, remain identical.
Message, raw-ID, and line mutation controls independently fail comparison.

| File | Line move | Diagnostic ID move |
|---|---|---|
| `web/composer/tools/sessions.py` | 643 → 644 | `2eadf525ccc1bee0` → `a440c9c9dd4b5ec8` |
| same | 2606 → 2643 | `05528467c87dc568` → `0474339f57beeded` |
| same | 2891 → 2928 | `dab951ad1c7b1c2f` → `8260fc1d81021247` |
| `web/execution/preflight.py` | 77 → 79 | `1ee6dbb2d8f8e1d5` → `e7904ee3ca6e1229` |

No signed metadata, source-binding metadata, test fingerprint, allowlist, or
suppression was changed. The parent's combined all-rule delta remains a
separate required check; the three-rule result makes no claim about it.

## Red/green and recovery record

The interrupted agent's original `red-handlers.log` is preserved. It reports
8 failures/2 passes but has no known original exit receipt; none is invented.
The recovered partial code contained a duplicated keyword, repaired before
the fresh meaningful admission run. `recovery-red` then exited 1 with
31 failures/64 passes, exposing missing bulk/patch/preview wiring alongside
draft fixture defects. Those fixture defects were corrected explicitly
(shallow provider patches require collection; adjacent RAG prefixes must not
collide), rather than accepted as security refusals.

`empty-owned-red` exited 1 with three expected missing-invariant failures.
Controlled removal of cache policy scoping and nullable-provider preservation
made `negative-control` exit 1 with the two intended behavioral failures and
nine passes; both protections were restored before freezing.
`guidance-red` exited 1 with twelve real legacy-preview repair-guidance
failures. Final frozen runs pass these controls.

The earlier 923-test regression exited 1 with 920 passes and three diagnosed
compatibility test failures: an embedded collision-policy positive omitted
new authority, an exception-injection lambda omitted the new keyword, and a
public-signature literal omitted the additive parameter. Their original
contracts were retained with corrected setups/expected API shape; the final
cohorts pass them. Full raw logs and explicit receipts remain in `d2-checks/`.

## Remaining combined acceptance

No full backend/PostgreSQL suite, all-rule trust-tier scan, provider request,
installation, signing, commit, Page update, or lifecycle implementation was
performed by this lane. Parent owns integration and the broader gates.

After adjacent02.17 supplies the real standalone portable lifecycle owner,
launch the public `elspeth-composer` stdio executable in a fresh subprocess
without pytest recovery injection. Initialize, author an in-root real file
source and sink, preview through actual bounded worker/plugin/graph checks,
independently prove escaped legacy paths refuse, and prove clean EOF joins
owned worker/preflight/watchdog work. Verify every claimed supported platform
under the separate lifecycle qualification. Until then fresh-process preview
success and full D2+02.17 completion remain unverified. Pytest's existing
executor-recovery fixture supports the isolated D2 preview controls only.

## Fix round 1 — D2-R1 explicit-null path bypass

This section supersedes the initial candidate for implementation/review status.
The original report, complete patch, manifests, and potentially affected source
bytes are preserved under `d2-fix1/original/`. The original review patch remains
byte-identical at its original path; it is not the fixed review artifact.

**Review artifacts for the fixed candidate:**

- Incremental change against the original reviewed source:
  `d2-path-admission-fix1.diff`, SHA-256
  `1f59ef6e606bcc8ec1c7027de99faa4e25a78afc4ee69fabbf59e19f79e5cd95`.
- Complete change against HEAD, including both new files:
  `d2-path-admission-fullreview-fix1.diff`, SHA-256
  `9796d6dbc518b1c67deeb27b68378d2dde96f641fd8d43d45ca76613c77c0d46`.
- Both reverse application checks and the whitespace check exited 0.
  `d2-fix1/artifact.json` identifies every file and checksum. Comparison of
  complete original/current manifests proves that this round changes only
  `_validation_authoring.py`, `preflight.py`, and the three test files
  `test_configured_root_admission.py`, `test_validation_authoring.py`, and
  `test_preflight_side_effects.py`.

### Reproduction and owning-boundary fix

The review finding was reproduced through the real loaded-session SDK handler
and bounded validation worker, with wrappers observing the real materializer
and real plugin constructor entry points. For **both source and sink `path:
null`**, a pre-existing `data/None` symlink to the synthetic test's
`outside.csv` produced a passed `path_allowlist`, a canonical **outside** path
in executable YAML, actual plugin-setup entry, and an overall valid runtime
preflight. `d2-fix1/reproduction.json` records the literal resolved locations;
complete per-case projections/verdicts remain under
`d2-fix1/red-handlers-tmp/*/observed-null-preview.json`.

The observed synthetic outside file's bytes were unchanged. This is **not** a
claim that no outside read occurred, or a universal no-I/O claim. The measured
reproduction establishes the unchecked outside path and plugin-setup reach.

The shared source/sink phase now tests key presence independently from value:
every present local-key alias must pass `is_local_path_value`, including null.
Missing keys remain absent. Only the documented nested-provider null exception
continues to skip path validation.

The runtime resolver now applies the same exact-nonempty-string/no-NUL check
before constructing `Path` and no longer coerces authored values with `str`.
Its existing generated-transform provider scope is unchanged; nested provider
null stays null, while other invalid provider values also refuse. Direct
materialization with a malformed value raises an intentional `ValueError`;
normal loaded preview refuses earlier through its typed, named path phase.
No exception catch, fallback path, migration, or state cleansing was added.

### Regression controls and real exits

The new loaded-handler matrix covers both source aliases (`path`, `file`) and
all three sink aliases (`path`, `file`, `persist_directory`), with and without
the pre-existing `None` escape link. Each case requires an actual failed
`path_allowlist` check, a non-valid preview, **no materialization or plugin-setup
entry**, and unchanged whole private/public state, saved bytes, content hash,
and version. A later plugin rejection cannot satisfy these assertions.

Additional controls cover every source/sink alias in both Web and MCP path
phases; all singular/plural source, sink, and provider resolver locations with
null/int/bool/list/mapping/empty/NUL values; direct executable and identity
materialization rejection; and absent-key/provider-null preservation through
both materializers. Provider null is excluded from the invalid-provider matrix
and retained by positive controls. Existing actual non-file and file preview
positives and Web tenancy controls remain in the final selection.

Exact red/green argv, both worktree source roots, selection strings and exits
are in `d2-fix1/red-green-commands.json`; complete final argv/exits are in
`d2-fix1/receipts.json`. All pytest runs used explicit `PYTHONPATH`, explicit
`-o pythonpath` for both roots, project-lane `--basetemp`, and `-n 0`.

| Check | Recorded result |
|---|---|
| New regression controls before production fix | exit 1; **135 failed, 5 passed**, 245 deselected |
| Observed legacy source/sink handler reproduction before fix | exit 1; **10 failed**, 165 deselected |
| Same new controls after fix | exit 0; **140 passed**, 245 deselected |
| Final affected MCP/path/validation/materialization/identity/preflight/leaf/attribute cohort | exit 0; **950 passed**, 114.13 s |
| Scoped ruff and format | both exit 0 |
| Scoped mypy on both modified production files | exit 0 |
| Whole contracts/census check | exit 0; no repin |
| Raw three-rule boundary scanner | exit 1; **205 findings**, matching clean-HEAD corpus semantically |
| Verified location-only boundary comparison and mutation controls | exit 0; no semantic additions/removals or mapping/derivation errors |

The raw aggregate runner remains exit 1 because the raw boundary scan is not
green. No boundary metadata was changed. Relative to the original D2 candidate,
the sole additional location-derived diagnostic change is
`preflight.py:79 → 80`, `e7904ee3ca6e1229 → b8b95ee95271dd35`, caused by the new
helper import; the bound test and recorded test fingerprint remain unchanged.
The comparison checks all original/candidate diagnostic fields and the exact
scanner ID formula, maps changed lines to unchanged source, and detects
independent message, ID, and line mutations. Evidence is in
`d2-fix1/boundaries.json` and `d2-fix1/boundary-location-delta.json` with explicit
exit receipts. The other three D2 location changes remain as reported above.

The final complete source/test freeze matches before and after: **6,207
inputs**, SHA-256
`991db7b0c8f38cacf2694421fc8785300455cf7ca1adbe14bf0d6852d536baf1`.
Evidence: `d2-fix1/before.json`, `after.json`, `frozen.txt`, and the final runner
log with import provenance. HEAD is still f9c23. Source is frozen for scoped
independent rereview; no commits or unrelated queue/lifecycle implementation
were performed. The original standalone lifecycle/combined acceptance limits
above remain in force. No long masquerade rerun, broad/full suite, all-rule
scan, signing, suppression, or Page update was performed in this fix round.
