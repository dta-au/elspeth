# D2 MCP configured-root path admission map

Read-only preparation, 2026-11-09. Source HEAD measured as
`5e192342c3fb025fd9a91244bebe437158e9de8e`. No production/test source edits,
providers, broad suites, or subagents. This lane note records preparation only;
implementation waits for parent dispatch. Read root `AGENTS.md`.

## Settled scope

User ruling supplied by parent: retain useful MCP file-backed authoring within
the configured data directory, reject escaping paths at admission. Do not
accidentally restrict MCP to invented Web session custody directories. Preserve
Web tenant confinement, preview refusal outside the configured root, public
export redaction, and provider authorship. Containment is distinct from file
existence. No new HTTP Composer MCP transport is needed.

Audit anchors: README register XC-10 / D2; `remediation-plan.md` R1;
`set_pipeline-N1`, `set_source-N6`, `patch_source_options-N6`,
`patch_node_options-N7`, `upsert_node` §7.1 probe 3, and output PO-N5. Current
code retains the audited cause: `composer_mcp/server.py:408` passes
`data_dir=None`; `require_data_dir_for_paths=True` protects only incremental
source calls, and bulk `set_pipeline` omits that flag at its source guards.

## Current ownership and call graph

| Owner/API | Current behavior | D2 implication |
|---|---|---|
| `composer_mcp.server.main()` | `--data-dir: Path`, default `data`; separate `--scratch-dir` for session/audit files | Data root comes from operator configuration, never tool arguments or scratch location. Update help currently describing preview-only checks. |
| `run_server(catalog, scratch_dir, data_dir)` → `_build_stdio_server(...)` | Only shipped Composer transport is stdio; creates `_StdioValidationSettings(data_dir)` | This is the production construction acceptance tests must drive. |
| `_build_stdio_server` | Generates synthetic 12-hex session token only for `validate_pipeline(..., session_id=token)`; passes async runtime callback and settings/snapshot/token hash into `create_server` | Token is preflight scoping, not filesystem authorization. Merely threading token plus data_dir still denies ordinary `input.csv`. |
| `create_server(catalog, scratch_dir, *, runtime_preflight, runtime_preflight_settings_hash, ...)` | Has no authoring data root; owns `SessionManager`, live state/baseline, recorder and actual MCP CallTool handler | Add explicit server-owned configured-root admission authority here and forward it through the same live handler. Existing embedded/direct callers must explicitly choose configured-root support or honest unavailable behavior. |
| `_dispatch_tool(name, arguments, state, catalog, scratch_dir, baseline=None, runtime_preflight=None, session_manager=None, session_checkout_ref=None)` | Creates trained-operator catalog/snapshot, calls `execute_tool(..., data_dir=None, require_data_dir_for_paths=True, validate_arguments=True, raise_schema_argument_errors=True)` | Do not overload `scratch_dir` as data authority. Preserve credential/schema/collision-policy checks. |
| `tools._dispatch.execute_tool(..., data_dir: str | None=None, session_id: str | None=None, require_data_dir_for_paths=False, ...)` | Constructs frozen `ToolContext` at lines 952–979 and invokes registered handler | Thread an explicit owned MCP-root authority through this existing seam rather than introducing a second mutation dispatcher. |
| `ToolContext` (`_common.py:3698`) | Carries Web `data_dir`, session engine/id/operation authority and optional proof services; no path-authority variant | Keep MCP filesystem authority distinct from Web blob/session authority and source-proof authority. |

Repository source references show `create_server` called only from its own
stdio builder; tests exercise embedded construction. The comment mentioning
in-process Web/MCP coordinator sharing is an integration facility, not a
shipped HTTP MCP endpoint. Web HTTP composer immediate and explicit-approve
paths use `tool_batch.py` and Web proposal replay uses
`sessions/routes/composer/proposals.py:492–518`; they independently pass actual
request `data_dir`, session identity, engine and operation authority into the
shared dispatcher. Preserve those call-site contracts.

## Reuse the actual confinement machinery, without widening Web

`web/paths.py` owns the local key sets and canonical OS path resolution:

* Source: `path`, `file`; `resolve_data_path` resolves relative to configured
  data_dir, absolute as written, resolving traversal and symlinks.
* Sink: `path`, `file`, `persist_directory`; provider nested path:
  `options.provider_config.persist_directory`.
* Web source authority is **only** `data_dir/blobs/<actual-session>/`.
  Web sink/provider authority is **only** that session's `outputs` and `blobs`
  subtrees. `session_id=None` yields no directories. Those shared functions
  must retain their current meaning.
* Web `resolve_sink_data_path` adopts logical `outputs/report.csv` under the
  actual session; `canonical_sink_local_paths` roots bare relative output
  spelling in `outputs/` and rejects `..` spelling. Preserve that behavior.

Smallest sound distinction: an owned configured-root authority created at the
MCP construction boundary, carried explicitly into `ToolContext` and preflight.
For MCP, use the configured root and `resolve_data_path` for the containment
decision (`resolved.is_relative_to(resolved_root)`), including sink/provider
keys, without synthetic session authorization/adoption. Continue using the
existing Web allowlists when this MCP authority is absent. Do not change
`allowed_source_directories` / `allowed_sink_directories` to interpret a
special session string or None as the deployment root. Nominally validate any
new owned authority; wrong owned type or contradictory Web/MCP context is an
internal contract error, not an apparently valid input refusal. Authority is
not a public schema field and cannot be supplied in tool arguments.

Resolve symlinks before containment: a link inside the root pointing outside
must fail. Current `web.paths` uses `Path.resolve()` and does **not** implement
a blanket ban on every symlink; do not claim it does. Explicitly test root
symlink policy and in-root symlinks, keeping the existing configured-root
interpretation instead of inventing a separate permissive fallback. No
`exists()` check is part of existing containment; a not-yet-created in-root
output remains authorable. A missing input is a later plugin/readiness issue.

## All direct path writers

Measured from shipped `_build_tool_defs()` and live mutation registry:

```text
MCP live path-writing names: ['patch_node_options', 'patch_output_options', 'patch_source_options', 'set_output', 'set_pipeline', 'set_source', 'splice_transform', 'upsert_node']
all nominated writers are live: True
MCP live mutation names: ['clear_source', 'patch_node_options', 'patch_output_options', 'patch_source_options', 'remove_edge', 'remove_node', 'remove_output', 'set_metadata', 'set_output', 'set_pipeline', 'set_source', 'splice_transform', 'upsert_edge', 'upsert_node']
```

| Writer | Existing gate / disposition |
|---|---|
| `set_source` | `sources.py:1130`: `_validate_source_path(..., require_data_dir=context.require_data_dir_for_paths)` before mutation; source-path refusal. |
| `patch_source_options` | `sources.py:2195`: same gate on complete merged options, including an encoding-only/no-op patch. |
| `set_output`, `patch_output_options` | `outputs.py:181,301`: `_validate_sink_path` on canonicalized/new merged options; sink-path refusal. |
| `upsert_node`, `patch_node_options`, `splice_transform` | `transforms.py:820,1930,2148`: `_validate_transform_provider_config_path` after plugin prevalidation; provider-path refusal. |
| `set_pipeline` | `sessions.py:1238,1451` both singular and plural sources; `1666` provider paths; `1811` all outputs. Source guards currently omit strict missing-root flag. Preserve candidate rejection, atomic replacement and inline-blob handling. |

`load_session` admits persisted state through the separate session dispatcher;
it does not author a new path option. Do not silently rewrite or discard old
session state. Record its compatibility behavior explicitly: previously saved
outside-root state must remain unable to preview/run/export raw paths, and
any subsequent path-writing mutation must validate its full proposed/merged
options. No blob/secret authoring tools should become exposed as a side effect.

## Preview and public export must stay coherent

`_build_stdio_server` calls `validate_pipeline`, whose phase at
`execution/validation.py:604` calls `validate_path_policy`
(`_validation_authoring.py:248`). That phase uses the Web session allowlists,
and later `resolve_runtime_yaml_paths` uses session-aware sink resolution.
Configured-root MCP authority must be honored consistently by this validation
and resolution path, while preserving Web defaults. Otherwise successful
admission of `input.csv` would still be followed by preview path refusal.
Keep strict-only MCP preview and actual bounded-worker execution; do not stub
preflight or weaken failed `path_allowlist` outcomes for escaped paths. Cache
identity must include any new authority mode/root affecting the verdict.

`generate_yaml` uses `generate_public_composition_dict` / public YAML projection
and intentionally strips source/sink/provider filesystem paths. It is read-only
and its projection must not overwrite private working state. Preserve those
contracts and existing credential controls. D2 does not authorize pipeline
generation, synthesis, fallback authorship, or tutorial-specific behavior.

## Typed errors and no-change oracle

Retain established recoverable path codes:
`source_path_policy_invalid`, `sink_path_policy_invalid`,
`provider_config_path_policy_invalid`, with actual rejected component ref.
Incremental tools use `_saved_graph_rejection_result`; `set_pipeline` uses
candidate-only rejection. Both preserve unchanged `updated_state` and empty
affected nodes (`_common.py:1360–1396`). MCP serializes this as a normal JSON
tool result with `success=False`; its recorder currently classifies ordinary
tool refusals as completed SUCCESS invocations, distinct from argument errors.
Do not accidentally turn path-policy refusal into a raw SDK exception.

Malformed Tier-3 path values must be admitted lexically before `Path()`:
exact nonempty strings, no NUL; integer/list/null/empty/NUL cases are currently
an XC-05 sibling risk once MCP starts supplying a root. Preserve documented
nullable provider-config semantics (`persist_directory=None` is skipped by the
current provider guard) where the plugin supports them. Use existing typed
argument/refusal channels intentionally; do not add broad exception wrapping.
MCP `ToolArgumentError` becomes `CallToolResult(isError=True)` with safe text and
ARG_ERROR audit status. A genuine owned configuration/store/resolution fault
must retain its owned failure/error identity, not be mislabeled as bad user
path input. Missing **authority** must fail closed with route-accurate guidance;
missing **file** does not by itself violate containment; wrong-context/type
is an owned invariant failure. Do not recommend unavailable MCP blob tools.

## Narrow acceptance/test map for implementation

1. Drive the actual registered CallTool handler from `_build_stdio_server`
   using a real catalog. In-root `input.csv` source succeeds for `set_source`
   and singular/plural `set_pipeline`; a non-path `null` source stays a positive
   control. Patch encoding on an admitted file source succeeds.
2. Exercise all eight writers above. Test both relative and absolute in-root
   paths, outside absolute paths, escaping traversal, and symlink escape;
   provider paths must cover patch and splice as well as upsert/bulk. Use real
   valid plugin options so an unrelated prevalidation refusal cannot satisfy
   the negative oracle. Keep relative output canonicalization behavior pinned.
3. For each refused mutation compare whole private state/content hash/version
   before/after through the live handler, plus session file bytes if saved.
   For full replacement include one good component and a later bad component
   to prove atomicity; no prepared inline-blob bytes/rows may be written by a
   later path refusal.
4. Missing authority on embedded construction/direct dispatch fails closed;
   wrong nominal authority, mixed Web/MCP context and missing/invalid configured
   root follow explicit owned contracts. Missing configured directory and
   missing leaf file must not be conflated with authority absence. Include
   malformed path values so enabling root checks cannot introduce TypeError/
   NUL ValueError escapes.
5. Preview in-root file authoring must reach real `path_allowlist`/plugin/graph
   checks; inject/legacy-load an escaped path and assert preview rejects by
   name. Export must omit private paths and leave private live state intact.
6. Web regression controls: real session A source and sink/provider positives;
   source root/shared directory, missing session and session B paths refuse;
   Web symlink/traversal controls stay red. Reuse
   `tests/unit/web/test_paths.py` and relevant runtime path tests rather than
   adding an HTTP transport.

Existing focused homes: `tests/unit/composer_mcp/test_server.py:545` pins the
old source refusal and must be deliberately revised;
`TestStdioServerConstruction` at 1318 pins old admission-then-preview sink
rejection and must become admission rejection plus independent preview guard
control. Keep CLI root propagation tests at 1454/1460. Audit behavior lives in
`test_call_tool_audit.py`; output redaction/private-state controls at
`test_server.py:840–1030`. Shared path tests already cover Web session, foreign
UUID paths, traversal and symlink escape. Runtime phase tests live in
`tests/unit/web/execution/test_validation_authoring.py`,
`test_validation.py`, `test_preflight_side_effects.py`.

## Current direct-handler controls and limitation

Fresh Python probes with `PYTHONPATH=src:elspeth-lints/src .venv/bin/python`
printed import `/workspace/elspeth/src/elspeth/__init__.py` and used the real
catalog + `_build_stdio_server` registered CallTool handler. Repository-local
files/audit scratch are under this lane's `d2-probe/`; no providers were called.
Completed control run exit 0:

```text
in-root set_source {'success': False, 'version': 1, 'errors': ['source_path_policy_invalid', 'no_source_configured', 'no_sinks_configured']}
in-root set_pipeline {'success': True, 'version': 2, 'errors': []}
in-root preview {'transport_error': 'Tool error: AuditIntegrityError'}
in-root export {'success': True, 'path_present': False}
outside set_source {'success': False, 'version': 1, 'errors': ['source_path_policy_invalid', 'no_source_configured', 'no_sinks_configured']}
outside set_pipeline {'success': True, 'version': 2, 'errors': []}
outside preview {'transport_error': 'Tool error: AuditIntegrityError'}
outside export {'success': True, 'path_present': False}
```

The initial instrumentation assumed every response was JSON and exited 1 on
that SDK error; the corrected run explicitly distinguishes safe transport
errors. A subsequent observation-only wrapper around error framing identified
`AuditIntegrityError('Shared executor recovery callback absent')`. This comes
from `web/async_workers.py:103/137`: fresh stdio construction does not register
the recovery callback. Source registration is in `web/app.py:1618`, while
`tests/conftest.py:690` injects one for pytest. Direct synchronous
`validate_pipeline` and `_dispatch_tool(...preview..., runtime_preflight=...)`
controls exited 0. Therefore the audit's historic pytest-backed runtime
path-refusal evidence is not fresh-process stdio preview verification at this
HEAD. Keep this lifecycle issue separate from D2 and report it to the parent;
do not silently fix it or claim preview passed from these probes.

### Public stdio entry verification (completed after parent request)

Verified the full entry chain: `pyproject.toml:271` maps `elspeth-composer` to
`elspeth.composer_mcp:main`; `.venv/bin/elspeth-composer` imports that function;
the package wrapper imports and calls `server.main`; `main` directly runs
`run_server` with asyncio; `run_server` builds the server then enters
`stdio_server` and `server.run`. There is no lifecycle initialization in those
wrappers. The autouse pytest fixture is
`tests/conftest.py:678–695` (`explicit_required_executor_recovery`) and sets the
callback at line 690, which explains the differing test environment.

Ran a fresh actual stdio child process via the MCP SDK's `stdio_client` and
`ClientSession`, initialized it and called real tools. No pytest fixtures,
monkeypatches, provider calls or replaced workers were involved. The command
was launched from the repository root with
`PYTHONPATH=src:elspeth-lints/src .venv/bin/python` as the controlling client;
child command and completed output (exit 0) were:

```text
public-command /workspace/elspeth/.venv/bin/elspeth-composer --scratch-dir /workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-probe/real-transport-scratch --data-dir /workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-probe/data
initialized True
set_source {'isError': False, 'success': True, 'version': 2}
set_output {'isError': False, 'success': True, 'version': 3}
preview_pipeline {'isError': True, 'text': 'Tool error: AuditIntegrityError'}
```

Positive controls used the legitimate non-file `null` source (`on_success=main`,
`on_validation_failure=discard`, observed schema) and CSV sink
`outputs/out.csv` with observed schema, `mode=write`,
`collision_policy=auto_increment`, `on_write_failure=discard`. Thus initialized
transport and actual mutation dispatch work while preview fails. The
observation-only framing wrapper in the separate in-process probe identifies
the original exception as the absent shared-executor recovery callback; the
public transport correctly scrubs it to its class. This is a verified adjacent
public-stdio preview blocker. Parent decides its tracker/scope; D2 must later
include fresh-process real-transport validation to avoid a pytest-fixture false
green.
