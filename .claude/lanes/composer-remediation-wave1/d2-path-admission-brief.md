# D2 implementation brief — configured-root MCP path admission only

Prepared 2026-11-09 against measured HEAD
`5e192342c3fb025fd9a91244bebe437158e9de8e`. Re-read current working files before
implementation because D1 work is proceeding serially in this checkout.
Inputs: user ruling as relayed by parent, `d2-mcp-path-map.md`,
`d2-design-review.md`, root `AGENTS.md`. This brief changes no code. Read
`CONTRIBUTING.md` whole-tree gates before editing. No providers are needed.

## Outcome and exclusions

Retain useful MCP file-backed authoring within the configured `--data-dir`;
refuse source, sink and nested provider paths outside that root **before**
mutation. Use the same canonical root and path interpretation during admission,
preview path validation, execution YAML materialization and identity YAML
materialization. Preserve Web session confinement, public path redaction,
credential controls, explicit sink collision policy, atomic candidate rejection
and provider authorship.

This is D2 only. Do not implement process/watchdog/recovery/coordinator teardown
changes, invent an HTTP MCP transport, expose blob/secret tools, add migrations,
rewrite old session files or change the planner/provider path. User has approved
substantive portable supervision under adjacent02.17, which remains a separate
ownership task. **Fresh-process public stdio preview passing is an outstanding
combined acceptance gate until that task lands.** Do not use pytest's injected
recovery callback to claim that gate passed.

## Narrow design

Create one frozen nominal owned authority (illustrative name
`McpConfiguredRootAuthority`) in `web/paths.py` or a small sibling module. Hold
the canonical configured root, issued only from server/operator configuration.
Use one canonical root for authoring, settings, cache identity and both YAML
resolution paths. It is not a session id, blob-custody/proof authority or public
tool argument. Do not introduce a generic strategy framework.

Thread one optional explicitly typed capability through:

```text
_build_stdio_server → create_server → registered CallTool handler
  → _dispatch_tool → execute_tool → ToolContext → existing path guards

_build_stdio_server runtime callback → validate_pipeline
  → _ValidationRunImpl / ValidationPipeline.run → _validate_pipeline_impl
  → validate_path_policy
  → materialize_validation_yaml → resolve_runtime_yaml_paths
  → _compiled_identity_settings → resolve_runtime_yaml_paths
```

MCP construction without root authority must fail closed for **every** local
path writer, including sink/provider/bulk paths; embedded callers that only
need non-file tools may keep working. Whether `create_server` makes the new
keyword required is a compatibility choice, not necessary to fix the defect.
Do not broaden general-purpose Web/direct dispatcher defaults merely to repair
MCP. Reject wrong owned authority types or contradictory MCP authority + actual
Web custody context as internal contract failures. Never treat `session_id=None`
or a magic synthetic id as deployment-root authorization.

## Path contract

Reuse the existing local-key constants and `resolve_data_path` from
`web/paths.py`. In the MCP branch resolve all local keys against the owned
canonical root; require canonical descendants with `is_relative_to(root)`.
Resolve absolute paths as written; allow absolute paths genuinely within root.
Reject traversal/symlink escapes. Do not insert synthetic session components
into MCP output/provider paths. Keep `canonical_sink_local_paths` spelling
normalization (`report.csv` → `outputs/report.csv`) and its traversal refusal.

In the Web branch keep `allowed_source_directories`,
`allowed_sink_directories` and `resolve_sink_data_path` semantics unchanged:
only the real session's blob subtree for sources, real session output/blob
subtrees for sink/provider, empty authority without a session. A deployment
root or another session's directory is not authorized Web storage.

Validate Tier-3 path shape before `Path()` to avoid introducing XC-05 crashes
when enabling root checks: intentional handling for exact nonempty strings,
NUL, integer/list/null/empty inputs. Retain supported nullable nested
`provider_config.persist_directory=None` semantics. Avoid broad catches that
turn owned storage/configuration errors into bad model input. Source/sink path
policy, plugin option validation and argument-shape error channels must remain
intentional and distinguishable.

Containment is not existence. A missing contained output remains authorable;
missing source files are diagnosed by the relevant later plugin/readiness
stage. A missing configured directory does not mean missing authority. Resolve
root symlinks under the existing canonical-root interpretation; a leaf symlink
resolving outside refuses. Revalidate paths at preview to catch changes after
admission; do not claim OS-race-free confinement.

## All writers and exact existing seams

The eight path writers were measured from shipped tool definitions/live
registry. Apply the authority consistently to the existing gates:

| Writer | Gate to migrate |
|---|---|
| `set_source` | `sources.py` `_validate_source_path`, currently around 1130 |
| `patch_source_options` | Same guard on complete merged options, around 2195 |
| `set_output`, `patch_output_options` | `outputs.py` `_validate_sink_path`, around 181/301 |
| `upsert_node`, `patch_node_options`, `splice_transform` | `transforms.py` `_validate_transform_provider_config_path`, around 820/1930/2148 |
| `set_pipeline` | `sessions.py` `build_set_pipeline_candidate`: singular and plural source guards around 1238/1451; provider guard around 1666; output guard around 1811 |

The `set_pipeline` source branches currently omit strict missing-root behavior;
fix both. All bulk path guards must finish before any prepared inline-blob
persistence or state replacement. Preserve candidate-only refusal for bulk and
saved-state refusal for incremental tools. Retain codes
`source_path_policy_invalid`, `sink_path_policy_invalid`,
`provider_config_path_policy_invalid`, and actual rejected component references.
Refusal must leave the whole private state, version/hash, affected nodes and
saved bytes unchanged. MCP refusal prose must describe its configured data
root and must not send the caller to unavailable blob tools.

`load_session` preserves persisted state; do not silently cleanse/rewrite it.
Legacy escaped state must still fail preview path validation and must never
export raw private paths. Patches validate their entire merged mapping, so a
non-path patch on a legacy bad path cannot bypass admission.

## Preview, identity and cache details

Relevant production files beyond tools:

* `execution/validation.py`: `validate_pipeline`, `_validate_pipeline_impl`,
  `_compiled_identity_settings` and its call site.
* `execution/_validation_pipeline.py`: `_ValidationRunImpl` and
  `ValidationPipeline.run` keyword propagation.
* `execution/_validation_authoring.py`: `validate_path_policy` needs the same
  nominal mode/root while retaining existing Web defaults and failure checks.
* `execution/_validation_materialization.py`: `materialize_validation_yaml`.
* `execution/preflight.py`: `resolve_runtime_yaml_paths`, called separately
  for execution and identity configurations. Both must resolve exactly the
  same output/provider location under MCP authority.

Do not broaden provider rewriting beyond its current generated `transforms`
scope merely because other nodes have options; `preflight.py` documents that
separate collector trigger. Preserve actual plugin/graph validation, strict-only
MCP preview and bounded worker dispatch. Replace stale synthetic-session
filesystem authorization in `_build_stdio_server`; a private nonce may remain
only for coordinator/cache scoping. Cache keys must distinguish canonical root
and authority mode, in addition to existing relevant settings/plugin snapshot.

Preserve `generate_public_composition_dict` / `generate_public_yaml` path
stripping and their current credential redaction. `generate_yaml` is read-only;
its public projection must never replace private working state. Fix the CLI
`--data-dir` help's preview-only description and stale builder comments when
the behavior changes. No broad audit-sheet rewrite is part of this code slice.

## Focused proof matrix

Use existing real MCP handlers and catalog rather than a parallel test-only
dispatcher. `_build_stdio_server` registered `CallToolRequest` handlers and the
existing test helper in `tests/unit/composer_mcp/test_server.py` are the primary
construction oracle. Tests may use the explicit existing pytest executor owner
for isolated D2 validation, but label the limitation.

| Contract | Positive control | Refusing/control case |
|---|---|---|
| File source authoring | Existing in-root relative and absolute CSV path; `set_source`; both `set_pipeline` source forms; subsequent encoding patch | Outside absolute; escaping traversal; in-root symlink to outside; no authority |
| All path writers | Real valid sink/provider options for each of eight tools; in-root paths | Same negative path families; merged patch on stored bad path; wrong authority type/mixed Web context |
| Atomic refusal | Existing nonempty graph and saved session; valid bulk component before later bad one | Whole private state/hash/version, saved file bytes and affected-node set unchanged; no prepared blob rows/bytes |
| Shape and missing storage | Intentional nullable provider path; missing contained output; configured root whose leaf does not yet exist | NUL/int/list/null/empty processed through intentional typed channel, no raw Path TypeError/ValueError; missing authority distinct from nonexistent path |
| Runtime path agreement | Direct real validation/materialization with MCP root; actual and identity YAML have identical resolved output/provider paths | Inject or load escaped legacy state, assert named `path_allowlist` failure independently of mutation refusal |
| Public export | Export succeeds for otherwise valid authoring | No private source/sink/provider paths in public YAML/adjacent public state; private live state unchanged |
| Web tenancy | Real session A source/output/provider directories | Session B/shared root/missing session/traversal/symlink escape remain refused |
| Cache | Same root/mode deduplicates as intended | Different canonical root/mode must not reuse a prior verdict |

Deliberately revise the old MCP test pin that all source file paths fail
(`test_server.py` around 545). Revise `TestStdioServerConstruction`'s old
admission-then-preview bad-sink behavior into admission refusal, retaining a
separate injected/legacy-state preview guard test. Keep CLI default/propagation,
schema/credential/collision-policy and audit tests intact. Ordinary semantic
path refusals remain completed tool results with `success=False`; malformed
arguments retain their established argument-error audit channel.

Focused existing homes: `tests/unit/composer_mcp/test_server.py`,
`test_call_tool_audit.py`; `tests/unit/web/test_paths.py`;
`tests/unit/web/composer/test_set_pipeline_candidate.py`, source/output/
transform rejection-contract tests; and `tests/unit/web/execution/`
`test_validation_authoring.py`, `test_validation_materialization.py`,
`test_validation_identity_document.py`, `test_preflight_side_effects.py`.

Choose necessary focused checks and whole-tree AST/lint/type gates from the
actual changed files under `AGENTS.md`/`CONTRIBUTING.md`; log lane-private command
outputs and explicit exit codes. Do not sign trust-tier metadata, run migrations
or claim broad suite results from focused tests. Do not change shared executor
semantics in order to make D2 tests green.

## Handoff and remaining combined gate

Report changed files, exact scoped verification exits and any remaining
failures. Review complete changed files for Web confinement and trust-domain
ownership. Parent integrates serially after D1; do not commit, push or update
shared issues unless separately dispatched.

Keep this explicit outstanding acceptance item in the D2 handoff:

> After adjacent02.17 supplies the real standalone portable lifecycle owner,
> launch the public `elspeth-composer` stdio executable in a fresh subprocess
> without pytest recovery injection. Initialize, author an in-root real file
> source and sink, preview through actual bounded worker/plugin/graph checks,
> independently prove escaped legacy paths refuse, and prove clean EOF joins
> owned worker/preflight/watchdog work. Verify every claimed supported platform
> under the separate lifecycle qualification. Until then fresh-process preview
> success and full D2+02.17 completion remain unverified.
