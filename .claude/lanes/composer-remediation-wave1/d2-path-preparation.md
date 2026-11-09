# D2 path admission — test preparation only

Prepared 2026-11-09 in the exclusively assigned worktree
`/workspace/elspeth/.claude/worktrees/composer-remediation-d2`.

Measured with `git rev-parse HEAD` (exit 0):

```text
14fef5e5342ccbd79b4fdd69b276c20f77d66dc1
```

Read the implementation brief, design review, MCP path map, worktree
`AGENTS.md`, `CONTRIBUTING.md` whole-tree gates, TDD skill and good-tests
reference before editing. No production file was edited. The parent retains
test capacity while the D1 full backend/PostgreSQL gates run.

## Exact execution status

**Draft tests only; no tests have been executed.** No pytest invocation,
collection, scan, lint, typecheck, build, install, provider call, HEAD/index
mutation, commit or push was performed. There is no baseline-green, red-test,
green-test or completion claim. Source inspection and file edits alone do not
establish that these draft tests collect or fail for the intended reason.

Read-only `git --no-optional-locks status --short` (exit 0) showed:

```text
 M tests/unit/web/composer/test_set_pipeline_candidate.py
 M tests/unit/web/test_paths.py
?? tests/unit/composer_mcp/test_configured_root_admission.py
```

## Draft test files and intended failures

### `tests/unit/composer_mcp/test_configured_root_admission.py`

Uses the real catalog, shipped `_build_stdio_server`, registered MCP SDK
`CallToolRequest` handler, real `SessionManager` persistence and real JSONL
audit sidecars. It imports no proposed/unimplemented authority API. The
existing splice graph fixture supplies a valid insertion topology; path
decisions, dispatch and persistence remain production behavior.

The first red run after release should select
`test_configured_root_admits_file_authoring` and
`test_each_writer_refuses_escape_atomically`, beginning with `set-source`
and `set-output` cases. These drive current existing APIs and compare actual
mutation outcomes. They must not be replaced by missing-import/signature-only
failures.

Draft matrix:

- All eight writers, with additional separate bulk singular-source,
  plural-source, output and provider cases. Positive relative/absolute paths;
  negative absolute, escaping traversal and leaf symlink paths. Bulk plural
  sources include an earlier valid non-file component before the bad source.
- Exact established path error code and actual rejected component. Refusal
  guidance must identify the configured root without unavailable blob advice.
  Existing bulk sink spelling refusal for `..` intentionally retains the
  earlier `plugin_options_invalid` channel.
- Whole live/public state, durable private state, content hash, version,
  affected nodes and saved bytes unchanged after refusal. Saving the live
  state after rejection independently detects mutations hidden by source
  response redaction. Semantic refusals retain successful-dispatch audit
  status and unchanged before/after versions.
- Embedded construction without configured authority refuses every local
  writer; a legitimate non-file `null` source remains authorable.
- NUL, integer, list, null and empty path cases must use intentional argument
  or policy channels, never raw Path TypeError/ValueError. Supported nullable
  provider persistence remains positive. Argument errors retain their audit
  category rather than being recorded as plugin crashes.
- Missing directory/leaf is distinct from missing authority. Root symlinks
  and contained leaf symlinks remain usable. Bulk output spelling stores
  `outputs/report.csv` without an invented MCP session segment.
- Legacy source/output/provider escape is preserved by loading but refused
  by the named preview `path_allowlist` check. Non-path merged patches cannot
  reauthorize the stored bad path. Preview rechecks a symlink changed after
  successful admission.
- Public YAML and adjacent public state omit private source/output/provider
  canaries while the live private state remains unchanged.
- In-root file preview must reach actual path, plugin-instantiation, graph
  and schema checks. Its pytest recovery fixture limitation is explicit in
  the module; this is not standalone stdio lifecycle evidence.

### `tests/unit/web/test_paths.py`

Adds shared production guard controls for source/sink/provider paths. Real
session A's blob subtree is positive; foreign session, shared deployment
root, traversal, leaf symlink escape and absent session are negative. Existing
session-output and resolver tests remain intact.

### `tests/unit/web/composer/test_set_pipeline_candidate.py`

Adds actual candidate/executor controls with real SQLite session/message
custody. A prepared good inline source followed by a bad output or provider
path must refuse with the relevant path code and leave no blob rows or bytes.
It asserts real DB/filesystem effects, not mocked persistence calls.

## Next phase after explicit parent release

1. Run the narrow existing-handler contrasts first and repair any draft
   setup mistakes until failures identify current path behavior. Capture
   commands, complete logs and exact exits under this lane.
2. Introduce the nominal authority only after meaningful red. Add direct real
   authority-type/mixed-Web-context and configured-root contract tests at that
   point, without using missing APIs as the primary regression proof.
3. Add direct real validation/materialization plus `_compiled_identity_settings`
   controls with literal expected source/output/provider paths. Source review
   confirmed the identity helper's caller runs only for a non-trained
   snapshot with profiled plugin IDs: stdio's trained-operator preview cannot
   prove this otherwise unreachable propagation. Test both resolution paths
   directly and retain profiled-caller coverage as needed.
4. Pin canonical-root and authority-mode cache separation, retaining real
   coordinator behavior. Revise the old `test_server.py` pins for unavailable
   source authority and admission-before-runtime bad sinks as the production
   contract changes. Keep credential/schema/collision and audit controls.
5. Select focused path/preflight/server suites and applicable whole-tree
   AST/masquerade/contracts/ruff/mypy/boundary/keyless-lint gates using actual
   changed files. Parent owns the full backend/PostgreSQL capacity; coordinate
   those gates rather than launching a competing broad suite.
6. Produce the eventual `d2-path-admission-report.md` and full review diff.

## Outstanding combined acceptance gate

After adjacent02.17 supplies the actual standalone portable lifecycle owner,
the public `elspeth-composer` executable still needs a fresh-process SDK stdio
acceptance run with no pytest recovery injection: real in-root file authoring,
actual preview checks, independent legacy escape refusal and clean EOF joining
owned preflight/worker/watchdog work, with each supported platform qualified.
No lifecycle implementation, invented MCP HTTP transport or provider call is
part of this D2 preparation.

**Waiting for explicit parent test-capacity release before any execution or
production edit.**
