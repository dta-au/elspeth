# Bounded remediation reconnaissance

Inspected main checkout read-only at `fix/composer-tool-remediation`, HEAD `6825994b4`. Read AGENTS.md and CONTRIBUTING.md (especially whole-tree gates). No product/test edits or full suite; this report is the only lane artifact. Parent implementation worktree: `.claude/worktrees/composer-remediation-wave1`.

## Current disposition and implementation

### Q1 / tracker 02.03 — still defective

`src/elspeth/web/sessions/routes/composer/proposals.py:491-516` replays `execute_tool` without `secret_wiring_policy`. Policy defaults absent and the secret handler denies it even if deployment settings authorize the tuple. Authoritative policy constructor is `runtime_secret_wiring_policy` in `elspeth.web.secrets.wiring_policy`; `_helpers.py:1954` already derives it from `settings.secret_wiring_allowlist` for runtime preflight.

Small fix: import constructor in proposals.py; pass `secret_wiring_policy=runtime_secret_wiring_policy(request.app.state.settings.secret_wiring_allowlist)` into this replay. Do not alter policy matching, preview admission, inline-marker behavior, or execution acknowledgement.

Add HTTP acceptance test using `tests/unit/web/sessions/test_routes.py` helpers `_make_app` (844), `_create_test_composition_proposal` (438), and the setup pattern of `test_accept_proposal_executes_tool_and_commits_state` (1178). Seed a valid current state and pending `wire_secret_ref` proposal, provide a resolver with `has_ref=True`, exact settings allowlist tuple, POST accept, assert 200/committed and persisted marker. Near-match option/plugin/secret or empty policy must return 422/rejected and preserve current state. Remove only the replay kwarg in a lane-private mutant: positive acceptance must turn red. Keep an unrelated accepted proposal control. Existing handler controls in `test_secret_wiring_collector.py` establish exact transform policy authorizes and no policy denies (both invoked successfully in-memory here); these are NOT HTTP route coverage.

Use a real plugin-declared credential option if checking real preflight. The audit's `passthrough/api_key` is not declared: with preflight stubbed, a 200 proves replay only. Document that limit, or use an appropriate production credential option and real preflight. Preflight stub pattern: `test_proposal_accept_preflight_failure.py:124-127` uses valid readiness. Do not introduce fake options to claim end-to-end runtime validity.

### GS-02 / tracker 12.07 — still defective

`tools/generation.py:2194-2216` exact and legacy-regex responses omit `error_code`; fuzzy branch (2222-2239) supplies it. Existing declaration (2269-2272) promises code when matched. In-memory dispatch via existing `_mock_catalog`, `_empty_state`, and test `execute_tool` produced:

```text
unknown_node_type             keys=[error_text, explanation, suggested_fix] error_code=None
LOG unknown_node_type END     keys=[error_text, explanation, suggested_fix] error_code=None
LOG UNKNOWN_NODE_TYPE END     keys=[error_text, error_code, explanation, suggested_fix] error_code=unknown_node_type
No source configured.         keys=[error_text, explanation, suggested_fix] error_code=None
ZZZ_UNRECOGNIZED_QQQ           keys=[error_text, explanation, suggested_fix] error_code=None
union_field_collision         keys=[error_text, explanation, suggested_fix] error_code=None
```

Implement additive exact canonical-code field and regex-record canonical code resolution using existing `_VALIDATION_GUIDANCE_BY_CODE`, without changing first-regex-match precedence or Expected-hint text. Preserve unknown and runtime-only `union_field_collision` omission. Avoid adding code merely because unrelated code text occurs elsewhere if the first matched guidance record differs. Test canonical code plus lower/upper noisy calls, legacy prose with an identifiable closed-code record, unknown/runtime reason controls, competing-regex precedence and Expected hint. Existing `_build_validation_guidance_index` (1940) binds legacy codes to first-matching tuple; reuse that authority rather than reimplement pattern ownership.

Tests: `tests/unit/web/composer/test_tools.py::TestExplainValidationError`; `test_validation_guidance_catalogue.py` (exact shape intentionally pinned at 255, precedence/hint controls 279-305); `test_generation_response_contracts.py` (producer `collect()` and `_contract()`), frozen `fixtures/generation_response_wire_4f206253.json`. Regenerate affected fixture bytes through `collect()`/contract serialization; inspect that only intended explanation cases gain code. Response contract already permits optional code; no tool argument or spec changes needed. Redaction allowlisting of data.error_code is an optional separate policy change, not required by this tranche; do not change it silently.

### Q25 / tracker 13.16 — still defective

`test_tools_discovery.py::TestDiscoveryTools::test_expression_grammar_documents_every_operator` (154-207) compares `token in operators_section`, so longer tokens mask missing short tokens. In-memory run of its actual method, then altered grammar returned through the module's existing get_expression_grammar binding:

```text
operator parity baseline passed
operator deletion < MISSED
operator deletion > MISSED
operator deletion / MISSED
operator deletion == CAUGHT
```

Change test parsing only: isolate Operators section; separate each line's token-list from description, split comma-delimited entries and strip whitespace while preserving multiword tokens `is not`/`not in`, then assert exact token membership. Preserve AST-token table completeness assertion and safe-constant checks. Do not split multiword operators into individual words, which would recreate masking. Ignore the ternary explanatory line or recognize its separate grammar; do not turn prose labels into operator tokens. Show the actual improved parity test fails for deletions `<`, `>`, `/`, `==`, `is not`, `not in` (last two are useful controls), and passes original grammar. No production grammar/parser change.

## Applicable checks

Run in isolated worktree, both roots exported, print imported package paths first. For every pytest run write lane-private log, wait for exit, inspect exit code. Example base: `PYTHONPATH="$WT/src:$WT/elspeth-lints/src" "$WT/.venv/bin/python" -m pytest -n 0 -o pythonpath="$WT/src $WT/elspeth-lints/src" ...` (set WT explicitly).

Focused selections:

- Q1 new route test plus `tests/unit/web/sessions/test_routes.py -k 'accept_proposal or accept_failed or accept_schema_stale'`, `tests/unit/web/sessions/routes/composer/test_proposal_accept_preflight_failure.py`, `tests/unit/web/composer/test_secret_wiring_collector.py`.
- GS-02 `tests/unit/web/composer/test_tools.py::TestExplainValidationError tests/unit/web/composer/test_validation_guidance_catalogue.py tests/unit/web/composer/test_generation_response_contracts.py tests/unit/web/composer/test_discovery_response_contracts.py`.
- Q25 `tests/unit/web/composer/test_tools_discovery.py::TestDiscoveryTools::test_expression_grammar_documents_every_operator` and sibling builtin/static grammar checks; run controlled deletion mutations against actual improved test.

Whole-tree obligations: ruff check/format touched files; mypy affected production files; `python -m scripts.check_contracts` for census/contracts; `tests/unit/web/test_sessions_composer_attribute_contracts.py`; `tests/unit/elspeth_lints/test_masquerade_gate.py` and `tests/unit/test_mock_discipline_baseline.py` (new tests participate). Capture before/after keyless `elspeth_lints.core.cli check --rules all --root src/elspeth` finding sets, compare actual delta, never hand-edit signatures. Boundary tests/scope/tier gate only if touching declarations or referenced tests. No spec/argument change proposed, so canonical-field inventory and frontend contracts need no intentional churn; avoid unnecessary snapshot regeneration. Q1 alters route replay wiring, not SQL/locks/persistence implementation; if implementation expands into those, PostgreSQL testcontainer gate becomes applicable. Parent chooses final suite scope under AGENTS.md before integration; this recon ran no full suite.

## Runtime palette remeasurement / tracker 01.01

Ran inventory.md producer recipe against live imports with both source roots, fail-closed proxy variables, `LITELLM_LOCAL_MODEL_COST_MAP=True`, and `ELSPETH_DISABLE_NETWORK=1`. Raw output summary:

```text
elspeth /workspace/elspeth/src/elspeth/__init__.py
elspeth_lints /workspace/elspeth/elspeth-lints/src/elspeth_lints/__init__.py
registered 41
canonical 43
loop openai_strict 45
planner openai_strict 23
loop none 45
planner none 23
web distinct 46
positive/negative palette controls passed
```

Both dialects emitted identical name sets. Positive control `list_models` exists on loop/planner; unknown name absent; planner `upsert_node` absent. Loop adds `handoff_to_user` and `restore_parked_graph` to canonical; planner adds its terminal `emit_pipeline_proposal`, producing 46 distinct web names. Canonical names include 41 registry tools plus async `request_advisor_hint` and `request_interpretation_review`. Planner emitted:

`diff_pipeline, explain_validation_error, get_audit_info, get_expression_grammar, get_pipeline_state, get_plugin_assistance, get_plugin_schema, list_models, list_sinks, list_sources, list_transforms, preview_pipeline, get_blob_content, read_attachment, get_blob_metadata, inspect_source, list_blobs, list_composer_blobs, list_secret_refs, validate_secret_ref, handoff_to_user, restore_parked_graph, emit_pipeline_proposal`.

No D1-D38 policy decision made. No claim of HTTP acceptance repair or suite success: observations are baseline introspection/control evidence only.

## MCP completion and combined route map / tracker 01.01

Follow-up measurement remained on main HEAD `6825994b482bc274df2ae85d8738b9078f0117b4`. It called the shipped `_build_tool_defs()`, created the actual MCP server with a real catalog and in-memory `BufferingRecorder`, invoked its registered `ListToolsRequest` and `CallToolRequest` handlers, and did not save a session or write an audit file. No agents, providers or suite launched. The SDK emitted its expected "not listed, no validation will be performed" warnings for the three deliberately excluded calls; actual dispatch rejected all three.

Reproduction command (run from repository root):

```bash
PYTHONPATH=src:elspeth-lints/src LITELLM_LOCAL_MODEL_COST_MAP=True ELSPETH_DISABLE_NETWORK=1 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 NO_PROXY= .venv/bin/python - <<'PY'
import asyncio, json
from collections import defaultdict
from pathlib import Path
from mcp.types import ListToolsRequest, CallToolRequest, CallToolRequestParams
from elspeth.composer_mcp.server import _build_tool_defs, create_server
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.pipeline_planner import planner_tool_definitions
from elspeth.contracts.composer_llm_audit import ToolContractDialect
producer = _build_tool_defs()
mcp_names = {item['name'] for item in producer}
assert len(mcp_names) == len(producer)
print('MCP producer', len(producer), sorted(mcp_names))
routes = {'freeform': {i['function']['name'] for i in composer_loop_tool_definitions(dialect=ToolContractDialect.NONE)}, 'planner': {i['function']['name'] for i in planner_tool_definitions(dialect=ToolContractDialect.NONE)}, 'MCP': mcp_names}
groups = defaultdict(list)
for name in sorted(set.union(*routes.values())):
    groups[tuple(route for route, names in routes.items() if name in names)].append(name)
for group, names in groups.items():
    print('+'.join(group), len(names), names)
async def probe():
    server = create_server(create_catalog_service(), Path('.claude/lanes/composer-remediation-wave1'), runtime_preflight=None, runtime_preflight_settings_hash=None, recorder=BufferingRecorder())
    listed = await server.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list'))
    live_names = {tool.name for tool in listed.root.tools}
    assert live_names == mcp_names
    print('MCP actual list handler', len(live_names), 'equals producer True')
    assert 'get_expression_grammar' in live_names and 'set_pipeline' in live_names
    for excluded in ['wire_secret_ref', 'emit_pipeline_proposal', 'zz_unknown_control']:
        assert excluded not in live_names
        result = await server.request_handlers[CallToolRequest](CallToolRequest(method='tools/call', params=CallToolRequestParams(name=excluded, arguments={})))
        payload = json.loads(result.root.content[0].text)
        print('MCP negative call', excluded, 'success', payload['success'], 'error', payload.get('error'))
        assert payload['success'] is False and payload['error'] == f'Unknown tool: {excluded}'
    result = await server.request_handlers[CallToolRequest](CallToolRequest(method='tools/call', params=CallToolRequestParams(name='get_expression_grammar', arguments={})))
    payload = json.loads(result.root.content[0].text)
    assert payload['success'] is True and 'Operators:' in payload['data']['grammar']
    print('MCP positive call get_expression_grammar success True Operators_present True')
asyncio.run(probe())
PY
```

Raw measured producer/handler output (exit 0):

```text
MCP producer 32 ['clear_source', 'delete_session', 'diff_pipeline', 'explain_validation_error', 'generate_yaml', 'get_audit_info', 'get_expression_grammar', 'get_pipeline_state', 'get_plugin_assistance', 'get_plugin_schema', 'list_models', 'list_sessions', 'list_sinks', 'list_sources', 'list_transforms', 'load_session', 'new_session', 'patch_node_options', 'patch_output_options', 'patch_source_options', 'preview_pipeline', 'remove_edge', 'remove_node', 'remove_output', 'save_session', 'set_metadata', 'set_output', 'set_pipeline', 'set_source', 'splice_transform', 'upsert_edge', 'upsert_node']
MCP actual list handler 32 equals producer True
MCP negative call wire_secret_ref success False error Unknown tool: wire_secret_ref
MCP negative call emit_pipeline_proposal success False error Unknown tool: emit_pipeline_proposal
MCP negative call zz_unknown_control success False error Unknown tool: zz_unknown_control
MCP positive call get_expression_grammar success True Operators_present True
```

Combined route groups from live producers (raw output; all 52 names partitioned exactly once):

```text
freeform+MCP 14 ['clear_source', 'patch_node_options', 'patch_output_options', 'patch_source_options', 'remove_edge', 'remove_node', 'remove_output', 'set_metadata', 'set_output', 'set_pipeline', 'set_source', 'splice_transform', 'upsert_edge', 'upsert_node']
freeform 9 ['create_blob', 'delete_blob', 'request_advisor_hint', 'request_interpretation_review', 'set_source_from_blob', 'set_source_from_blobs', 'update_blob', 'wire_blob_inline_ref', 'wire_secret_ref']
MCP 6 ['delete_session', 'generate_yaml', 'list_sessions', 'load_session', 'new_session', 'save_session']
freeform+planner+MCP 12 ['diff_pipeline', 'explain_validation_error', 'get_audit_info', 'get_expression_grammar', 'get_pipeline_state', 'get_plugin_assistance', 'get_plugin_schema', 'list_models', 'list_sinks', 'list_sources', 'list_transforms', 'preview_pipeline']
planner 1 ['emit_pipeline_proposal']
freeform+planner 10 ['get_blob_content', 'get_blob_metadata', 'handoff_to_user', 'inspect_source', 'list_blobs', 'list_composer_blobs', 'list_secret_refs', 'read_attachment', 'restore_parked_graph', 'validate_secret_ref']
```

This reconciles freeform45, planner23, MCP32, Web46, MCP/Web shared26, MCP-local6. Exposure measurements cover the requested route inventory only; they do not prove every tool invocation's prerequisites or behavior.

## Audit-baseline comparison — these three candidates only

Compared each scoped file's actual git blob at audit baseline `3b28d537be98234da8135c418bf185a998c18b68`, at current HEAD, and the live working file using `git rev-parse REF:path` and `git hash-object path` (not a regex). Raw equality results:

```text
path                                                        audit blob                               HEAD blob                                live blob                                all equal
src/elspeth/web/sessions/routes/composer/proposals.py         cc66debb9546505db1231a48c505fb75798bc74a cc66debb9546505db1231a48c505fb75798bc74a cc66debb9546505db1231a48c505fb75798bc74a True
src/elspeth/web/composer/tools/generation.py                  06a93f4a190df3fc614f2de8339a745c0d1c6174 06a93f4a190df3fc614f2de8339a745c0d1c6174 06a93f4a190df3fc614f2de8339a745c0d1c6174 True
tests/unit/web/composer/test_tools_discovery.py              9aa200b096aede02588556ede63473c7f1cc07ab 9aa200b096aede02588556ede63473c7f1cc07ab 9aa200b096aede02588556ede63473c7f1cc07ab True
tests/unit/web/composer/test_validation_guidance_catalogue.py 8a9fc03846794ebe2b911d7dc9751a6abfc8e93b 8a9fc03846794ebe2b911d7dc9751a6abfc8e93b 8a9fc03846794ebe2b911d7dc9751a6abfc8e93b True
tests/unit/web/composer/test_generation_response_contracts.py ce2427c176487bbcc709a2c95e5e2066788c8ebc ce2427c176487bbcc709a2c95e5e2066788c8ebc ce2427c176487bbcc709a2c95e5e2066788c8ebc True
tests/unit/web/composer/fixtures/generation_response_wire_4f206253.json 72ff7ce20a8d2dbd0739ad893fb101b7ede5567a 72ff7ce20a8d2dbd0739ad893fb101b7ede5567a 72ff7ce20a8d2dbd0739ad893fb101b7ede5567a True
tests/unit/web/sessions/test_routes.py                        3273e5c77ee54446661df49c899beaac19b513b4 e40ed8bbf9e0a8cf2d2dd59a28ce9abe498d9692 e40ed8bbf9e0a8cf2d2dd59a28ce9abe498d9692 False
```

The scoped comparison's positive control is `test_routes.py`, whose blob genuinely moved; known unchanged production files are equality controls. Read the actual `git diff --unified=3 BASE HEAD -- tests/unit/web/sessions/test_routes.py`: its sole hunk changes the worker-entered hang guard at 5578 from 5 seconds to 30, with rationale comment. The Q1 accept fixture/test at 1178 is unchanged. Consequently the three candidates' audited implementation defects remain applicable; this is not global R0 completion and makes no disposition for other audited files or D1-D38 policies.
