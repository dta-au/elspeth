# `wire_secret_ref` — authorize and author a credential marker

Assessment baseline: `3b28d537be98234da8135c418bf185a998c18b68` (0.8.9). Reconnaissance/design only. **Overall confidence: High for source and focused authorization/review tests.**

> **Review pass (2026-11-09).** A second lane re-verified every link and claim against the baseline source and added §3.1, §5.1 and §6.1. Corrections to the first pass are marked *Corrected* in place. Findings marked *demonstrated* were shown by short in-memory handler/dispatch runs (real `create_catalog_service()` catalogue, trained-operator snapshot, the metadata-only `_ReferenceInventory` test double, no database, no provider, no network). They are not end-to-end service, browser or live-model runs. Nothing proposed here has been implemented.

## 1. Purpose and usage

Select this tool after discovering an available reference and a plugin-owned credential option that the deployment explicitly authorizes. It writes a marker, never the secret value. It cannot grant policy permission or approve a run. The destination component must exist; the reference must resolve for the current authenticated user. Requires the scoped resolver, user ID, and server-authored wiring policy. It is web-only and absent from Composer MCP.

The agent has **no discovery surface for authorized destinations**: `list_secret_refs`/`validate_secret_ref` report availability only, and nothing in the composer tool or prompt surface projects `secret_wiring_allowlist` (grep for `secret_wiring\|wiring_allowlist` under `src/elspeth/web/composer/` finds only the handler, a runtime-preflight note and a settings protocol accessor). Whether a wire will be authorized is therefore learned only by attempting it.

## 2. Contract

Required `name: string`, `target: source|node|output`, and `option_key: string`; optional `target_id` is required for node/output and for source when more than one source exists. With one source, omitted `target_id` selects that source. *Corrected:* null handling is dialect-dependent. In the canonical schema and the `none` dialect an explicit `null` is kept and then refused by [`_OmittableString`](../../../../src/elspeth/web/composer/redaction.py#L1428); omit the field instead. In `openai_strict` the wire projection promotes `target_id` to required-and-nullable, and [`decode_wire_arguments`](../../../../src/elspeth/web/composer/tools/wire_projection.py#L930) maps `null` to omission (measured: strict `null` → `wire_conformant=True`, strict omission → `wire_conformant=False`; `none` is the reverse). Extra fields reject. `option_key` names one literal top-level key; a dotted value is stored as a single key containing a dot, not as a nested path (see N4). `target_id` is the **bare** name (`clean`, `main`, `records`), not the `node:`/`output:`/`source:` component-id spelling the tool itself reports (N5).

The tool verifies reference availability, target existence, exact server policy tuple `(secret, component_type, plugin, option_key)`, and secret placement. *Corrected:* placement is not plugin-approved. [`_secret_ref_placement_error`](../../../../src/elspeth/web/composer/tools/_common.py#L3280) applies the shared credential field-name heuristic (`SECRET_FIELD_NAMES` plus `_key`/`_token`/`_secret`/… suffixes) and one plugin-specific exception, the database sink's `url` ([ref_policy](../../../../src/elspeth/web/secrets/ref_policy.py#L9)). It does not consult the plugin's declared `discovery_secret_requirements` or the plugin config model, so an authorized option the plugin does not declare is accepted (N3). Node targets are restricted to transform/aggregation/collector plugin nodes; these use `component_type=transform` for policy. Output targets use `sink`. Source/output AWS endpoint policy is rechecked. It sets `options[option_key]={"secret_ref": name}`, silently replacing whatever was there (N2), reconciles authoritative reviews, checks canonical interpretation invariants, and returns affected component, validation/delta, and applied-component echo after a composition version increment. `data` is `None` on success. Success reports `affected_nodes` as `source` / `source:<name>` / bare node id / bare output name, while refusals report `rejected_component` as `source` / `source:<name>` / `node:<id>` / `output:<name>` (pinned by [the success-shape test](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L377) and the refusal table at [L274](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L274)). It does not resolve the secret or execute a plugin.

This is uncached `SECRET_MUTATION` with graph/validation/YAML effects. *Corrected:* in explicit-approval mode the graph edit is proposed **without running the handler**, and accepting the proposal currently always fails authorization because the accept route omits the wiring policy (N1); any eventual credential-use acknowledgement is a separate out-of-band run gate. *Corrected:* repeating an identical wire **does** advance the version (demonstrated: v7 → v8 → v9 with byte-identical options; SG-03 family). Missing context, unavailable ref, missing/unsupported target, destination denial, disallowed placement, endpoint or review failure returns a typed refusal with prior state preserved. Denied exact policy cannot be fixed by retries. Shape errors raise safe argument errors; owned faults propagate. An empty `option_key` is not a shape error: it reaches authorization and is denied as `unauthorized_secret_ref` for option `''` (demonstrated, N6).

## 3. Implementation trace

The [argument model](../../../../src/elspeth/web/composer/tools/secrets.py#L75), [handler](../../../../src/elspeth/web/composer/tools/secrets.py#L323) and [declaration](../../../../src/elspeth/web/composer/tools/secrets.py#L537) own authoring semantics. [`secret_wiring_authorization_error`](../../../../src/elspeth/web/secrets/wiring_policy.py#L131) enforces exact destination policy (`None` policy denies); [placement helpers](../../../../src/elspeth/web/composer/tools/_common.py#L3280) use the field-name heuristic and [ref policy](../../../../src/elspeth/web/secrets/ref_policy.py#L14). [`_reconciled_secret_wire_result`](../../../../src/elspeth/web/composer/tools/secrets.py#L287) reopens/refuses stale review evidence and enforces canonical requirements.

*Corrected link:* the [registry](../../../../src/elspeth/web/composer/tools/_registry.py#L94) (secrets plane spread at [L101](../../../../src/elspeth/web/composer/tools/_registry.py#L101); the former `#L111` pointed into `response_contract_for`), [canonical model definitions](../../../../src/elspeth/web/composer/tools/_dispatch.py#L311) (pinned last for the prompt-cache marker, [L360](../../../../src/elspeth/web/composer/tools/_dispatch.py#L360)), provider wire projection, and [public dispatcher](../../../../src/elspeth/web/composer/tools/_dispatch.py#L795) connect schema/context admission. [Tool-batch approval and worker execution](../../../../src/elspeth/web/composer/tool_batch.py#L1728), [audit](../../../../src/elspeth/web/composer/audit.py#L812), and [frontend cards](../../../../src/elspeth/web/frontend/src/components/chat/ToolCallCard.tsx#L75) publish the authored edit. [Execution secret validation](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) rechecks markers introduced by any route; [run admission](../../../../src/elspeth/web/execution/service.py#L2548) requires state-bound out-of-band acknowledgement before execution. Tool arguments cannot supply that authority.

### 3.1 Route inventory

Instrument: read-only introspection with both source roots on `PYTHONPATH`; `elspeth.__file__` resolved into the worktree. Measured: `wire_secret_ref` is index 44 of 45 and last in `composer_loop_tool_definitions` for both dialects; absent from `planner_tool_definitions` in both dialects (positive control `validate_secret_ref` present); absent from MCP `_build_tool_defs()` (32 tools; positive control `set_pipeline` present). Callers were found by `grep -rn "wire_secret_ref\|_execute_wire_secret_ref\|SECRET_MUTATION" src/` and `grep -rn secret_wiring_policy src/elspeth`.

| Route | Reaches `wire_secret_ref`? | Gate / notes |
|---|---|---|
| Canonical registry / emitter | yes | [`_REGISTERED_TOOLS`](../../../../src/elspeth/web/composer/tools/_registry.py#L101) → `_SECRET_MUTATION_TOOLS`; `get_tool_definitions()` emits it last ([L342](../../../../src/elspeth/web/composer/tools/_dispatch.py#L342)); import-time trailing-pin check [L360](../../../../src/elspeth/web/composer/tools/_dispatch.py#L360) |
| Freeform loop | yes, both dialects | [`composer_loop_tool_definitions`](../../../../src/elspeth/web/composer/provider_gateway.py#L391) keeps it last after recovery controls; no phase/policy filter found |
| Public dispatch context guard | refuses without context | [`_requires_secret_context`](../../../../src/elspeth/web/composer/tools/_dispatch.py#L679) → `secret_context_unavailable` before the handler; `secret_wiring_policy` threads into `ToolContext` at [L962](../../../../src/elspeth/web/composer/tools/_dispatch.py#L962) and defaults to `None` (deny) |
| Freeform dispatch, `auto_commit` | executes | [`execute_tool`](../../../../src/elspeth/web/composer/tool_batch.py#L3008) with `secret_wiring_policy=ctx.custody.secret_wiring_policy` ([L3021](../../../../src/elspeth/web/composer/tool_batch.py#L3021)), custody built in [service](../../../../src/elspeth/web/composer/service.py#L1646) from `settings.secret_wiring_allowlist`; incremental required-control finalization follows on valid success. Async compose operations run the same turn ([worker](../../../../src/elspeth/web/sessions/composer_async_worker.py#L816)); no tool-specific async path |
| Batch dependency refusal | can skip it | [`referenced_resources`](../../../../src/elspeth/web/composer/batch_dependencies.py#L115) derives `(target, target_id or "source")`; [consumer](../../../../src/elspeth/web/composer/tool_batch.py#L1551) refuses with `batch_dependency_refused` when an earlier refused call promised that resource (N7) |
| Freeform dispatch, `explicit_approve` | **staged without handler run** | [Approval predicate](../../../../src/elspeth/web/composer/tool_batch.py#L1728) uses `is_mutation_tool` (includes `SECRET_MUTATION`), but the pre-proposal preview runs only for `_MUTATION_TOOLS` ([L2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057)), the core `MUTATION` map. No availability, target, authorization, placement or review check runs before [proposal creation](../../../../src/elspeth/web/composer/tool_batch.py#L2179); the model receives `APPROVAL_REQUIRED` ([L2222](../../../../src/elspeth/web/composer/tool_batch.py#L2222)) |
| Proposal summary / diff | generic | [`build_tool_proposal_summary`](../../../../src/elspeth/web/composer/proposals.py#L122) has no wire branch: "Apply composer tool wire_secret_ref."; [`TOOL_PROJECTORS`](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L557) has no `wire_secret_ref` key, so structured arguments render |
| Proposal accept route | **always denied** | [`execute_tool`](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L493) passes `secret_service`/`user_id` but no `secret_wiring_policy` (control grep: 0 policy hits, 4 `secret_service` hits in the file), so the handler denies every wire; the [`not result.success` branch](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L563) auto-rejects with [422 `proposal_validation_failed`](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L621). The later state-data validation does pass the policy ([helpers](../../../../src/elspeth/web/sessions/routes/_helpers.py#L1954)); only the tool replay is policy-less (N1) |
| Runtime preflight / execution | rechecks all markers | [`ComposerPreflight`](../../../../src/elspeth/web/composer/composer_preflight.py#L141) and [`validate_secret_evidence`](../../../../src/elspeth/web/execution/_validation_authoring.py#L715) authorize every authored marker by `field_path`; [completion note](../../../../src/elspeth/web/composer/composition_completion.py#L813) tells the model `unauthorized_secret_ref` is operator policy |
| Credential guard / diagnostics | argument projection | [`credential_guard`](../../../../src/elspeth/web/credential_guard.py#L258) renames `option_key` to `structural_option_name` before credential scanning; [diagnostic evidence](../../../../src/elspeth/web/composer/diagnostic_evidence.py#L379) treats it as structural |
| Redaction manifest | yes | [Entry](../../../../src/elspeth/web/composer/redaction.py#L4058), [reason](../../../../src/elspeth/web/composer/redaction.py#L3627) |
| Progress events | yes | [`tool_started_progress_event`](../../../../src/elspeth/web/composer/progress.py#L582) labels it as a read ("checking available secret references") (G4) |
| Default planner | no | Not emitted; the planner receives `secret_wiring_policy` only for candidate custody ([L3432](../../../../src/elspeth/web/composer/pipeline_planner.py#L3432)) |
| Recovery controls | no | `recovery_controls.py` has no reference (grep count 0) |
| Composer MCP | no | [`_build_tool_defs`](../../../../src/elspeth/composer_mcp/server.py#L210) excludes secret tools ([L116](../../../../src/elspeth/composer_mcp/server.py#L116)) |
| REST | none | [`secrets/routes.py`](../../../../src/elspeth/web/secrets/routes.py#L70) manages inventory/values only; no wiring route |
| Frontend | renders only | [`toolCallDescriptions`](../../../../src/elspeth/web/frontend/src/components/chat/toolCallDescriptions.ts#L94) labels it as mutating |

No tutorial-specific branch was found for this tool (the grep above lists only these sites).

## 4. Alternative paths

Single source can be selected implicitly; plural sources require exact identity. A structural gate/coalesce-like node cannot receive plugin credentials. Available reference plus a near-match policy still denies before placement/endpoint processing. Authorized placement mutates only the intended options and reconciles review state; invalidated review anchors reopen or the edit refuses. Inline markers introduced through patch/YAML/whole-pipeline paths are revalidated before execution, so this tool is not the sole enforcement seam. Those inline paths are **not** authorization-checked at authoring time — `ToolContext.secret_wiring_policy` has exactly one consumer, this handler — so an unauthorized inline marker is accepted into state and surfaces only at runtime preflight, whereas the same destination through `wire_secret_ref` is refused with state preserved. Operator-profile-lowered credentials have a separately server-authored trust path and are not model wiring grants.

## 5. Usability assessment

No authorization bypass was demonstrated; N1 fails closed, not open. Tests explicitly show available credentials do not authorize near-match destinations. **Intended restrictions:** deny-by-default exact policy and separate run acknowledgement remain essential, even when a planner cannot complete the user's request without operator changes. Workaround: surface the exact blocked capability and request the relevant operator action; never fabricate credentials, alter destination spellings to evade policy, or interpret a successful wire as execution approval.

**Source-established guidance limitation:** `target_id` schema prose mentions node/output but omits the multi-source requirement, which is enforced only in handler semantics. The larger structural issue is that destination capability discovery, placement, candidate mutation, review reconciliation, preflight, and run acknowledgement are separate contracts the orchestrator must join correctly — and in explicit-approve mode the join is currently broken at proposal creation and acceptance (N1).

### Findings (review pass)

| ID | Classification | Finding | Where |
|---|---|---|---|
| wire_secret_ref-N1 | Source-established route defect; handler consequence demonstrated | Proposal acceptance replays the tool without `secret_wiring_policy`, so every accepted wire is denied and auto-rejected (422), whatever the allowlist says. Same call with/without the policy: `(True, v8)` vs `(False, v7, unauthorized_secret_ref)`. Paired with the explicit-approve preview gap ([L2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057)) the model is told `APPROVAL_REQUIRED` for a wire that can never apply. No test couples `wire_secret_ref` with acceptance (`comm` of the two grep sets: only an unrelated JSON fixture) | [proposals.py L493](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L493) |
| wire_secret_ref-N2 | Demonstrated | Wiring over an existing different marker (`OTHER_REF`) succeeds silently; `data` is `None` and the replaced reference name appears nowhere in the result | [secrets.py L467](../../../../src/elspeth/web/composer/tools/secrets.py#L467) |
| wire_secret_ref-N3 | Demonstrated at authoring; execution consequence unverified | An authorized wire into an option the plugin does not declare (`passthrough` / `api_key`) returns `is_valid=True`, no warnings. `_prevalidate_plugin_options` withholds marker keys from config-model validation, so even sibling prevalidation accepts it, while the literal form is refused "Extra inputs are not permitted". Requires an operator rule naming the undeclared option, so practical risk is bounded by operator configuration | [_common.py L2589](../../../../src/elspeth/web/composer/tools/_common.py#L2589) |
| wire_secret_ref-N4 | Demonstrated aliasing; impact hypothesis | Dotted `option_key` (`auth.api_key`) authorized by a dotted rule is stored as a literal top-level key; placement passes via the `_key` suffix. `collect_secret_ref_marker_sites` yields `field_path="auth.api_key"` for both the literal key and a genuinely nested marker, so one rule authorizes both shapes at execution. The tool cannot author the nested shape the [policy docstring](../../../../src/elspeth/web/secrets/wiring_policy.py#L41) describes | [core/secrets.py L343](../../../../src/elspeth/core/secrets.py#L343) |
| wire_secret_ref-N5 | Demonstrated | The tool's own output vocabulary is not accepted as input: `target_id="source:records"` → `source_not_found`, `target_id="node:clean"` → `node_not_found`, although success reports `source:records` and refusals report `node:clean` | [secrets.py L381](../../../../src/elspeth/web/composer/tools/secrets.py#L381) |
| wire_secret_ref-N6 | Demonstrated | Empty `option_key` (and unvalidated `name` shape) reach authorization instead of failing shape admission; the argument model has no `min_length` and does not reuse `validate_secret_name`, which `SecretWiringRuleSettings` applies to the policy side | [secrets.py L92](../../../../src/elspeth/web/composer/tools/secrets.py#L92) |
| wire_secret_ref-N7 | Hypothesis | `referenced_resources` defaults omitted `target_id` to `"source"` for every target, while the handler resolves "the only source" by count; a refused same-batch `set_source` for `source` could block a wire that would have resolved to a differently named single source, and node/output omissions are keyed as resource `node:source`/`output:source`. Not exercised | [batch_dependencies.py L115](../../../../src/elspeth/web/composer/batch_dependencies.py#L115) |
| wire_secret_ref-N8 | Source-established | Plugins already declare credential fields (`discovery_secret_requirements`, surfaced to the model as `secret_requirements`), but placement ignores them in favour of the name heuristic, so plugin-owned credential vocabulary and wire placement can disagree | [base.py L395](../../../../src/elspeth/plugins/infrastructure/base.py#L395) |

### 5.1 Guidance parity

Instrument: `grep -n "wire_secret_ref\|secret_ref\|list_secret_refs\|validate_secret_ref\|secret_wiring\|allowlist"` and `grep -ni "unauthorized\|deny-by-default\|secret_requirements\|post_hoc"` over both skill files, `prompts.py` and `pipeline_planner.py`. Control: `set_pipeline` matches 31 and 2 times in the two skills; `advisor` matches 11 times in `pipeline_composer.md`.

| ID | Problem | Evidence |
|---|---|---|
| G1 | Neither skill teaches the tool beyond the autogen inventory line. Nothing says wiring is deny-by-default, that availability is not authorization, that `unauthorized_secret_ref` is terminal for the agent, or that target `node` is policy component `transform`. Only the declaration and the runtime-preflight note ([L813](../../../../src/elspeth/web/composer/composition_completion.py#L813)) say so. The skill should: it is the one refusal the model cannot repair | [`pipeline_composer.md` L345](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L345); [`pipeline_capabilities.md` L88](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L88) teaches availability checks only |
| G2 | The declaration names the tuple as "secret/component/option"; the enforced tuple, the denial message and the `unauthorized_secret_ref` guidance all include **plugin** | [secrets.py L541](../../../../src/elspeth/web/composer/tools/secrets.py#L541) vs [wiring_policy.py L151](../../../../src/elspeth/web/secrets/wiring_policy.py#L151) |
| G3 | `target_id` description omits the multi-source source case (first pass) and the bare-name requirement (N5); in `openai_strict` "(required for node/output targets)" is misleading because the field is always required there, with `null` meaning omit | [secrets.py L555](../../../../src/elspeth/web/composer/tools/secrets.py#L555); measured strict schema |
| G4 | User-facing progress describes a running wire, a graph mutation, as "I'm checking available secret references" / "Checking available secret references without reading secret values" (GS-03 class) | [progress.py L582](../../../../src/elspeth/web/composer/progress.py#L582), [L644](../../../../src/elspeth/web/composer/progress.py#L644) |
| G5 | Credential-repair guidance steers the model to inline markers in `set_pipeline`/`upsert_node` first, with `wire_secret_ref` as the post-hoc path. That is correct for new nodes, but no text says the inline path defers authorization to runtime preflight, while the tool refuses up front; a model choosing between them cannot know which gives the earlier, state-preserving answer | [_common.py L1976](../../../../src/elspeth/web/composer/tools/_common.py#L1976); `pipeline_composer.md` L483, L523 |
| G6 | No guidance mentions that in explicit-approve mode a wire is staged unchecked (N1). Today the model is told `APPROVAL_REQUIRED` and cannot know it will be refused on acceptance | [tool_batch.py L2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057) |

The per-code static guidance from `explain_validation_code` for all twelve refusal codes was printed and read; it is accurate, and `secret_target_required` already names the multi-source case. The skills contain no stale references to removed tools or arguments for this tool.

## 6. Structural remediation

Use a shared typed destination descriptor and value-free authorization/capability view across schema discovery, list/validate references, wiring, whole-state admission, and execution disclosure. Plugin contracts should own supported credential fields (the existing `discovery_secret_requirements` declaration is the natural seed, N8); web policy owns destination permission; review authority owns invalidation; execution owns approval and actual resolution. Keep all alternate marker-entry paths under the same enforcement boundary and make each outcome actionable. Give the policy and the marker walk one unambiguous path encoding, so a literal dotted key and a nested key cannot alias (N4). Every replay of a secret mutation — proposal preview and acceptance included — must receive the same server-authored policy as live dispatch (N1; GS-01 family, R1).

Acceptance: source/node/output/aggregation/collector targets obey one destination vocabulary; multi-source omission rejects with explicit guidance; exact authorized tuple succeeds, every near-match denies; plugin placement and owned-field constraints cannot be bypassed through patch/YAML/blob markers; review evidence refreshes consistently; a successful wire cannot mint a run acknowledgement; real values never enter tool/audit payloads; an authorized wire accepted in explicit-approve mode applies, and an unauthorized one is refused before it becomes a proposal. Compatibility risk is Medium for destination descriptor/interface changes, Low for correcting schema guidance. Preserve existing no-value, exact allowlist, and out-of-band approval contracts.

### 6.1 Simple enhancements

| # | What | Where | Why | Acceptance check |
|---|---|---|---|---|
| E1 | Pass `secret_wiring_policy` (from `settings.secret_wiring_allowlist`, as `_helpers.py` L1954 does) to the accept-route `execute_tool` | [proposals.py L493](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L493) | Closes N1's acceptance half | New route test: allowlist authorizing `(ROUTE_REF, transform, passthrough, api_key)`, an explicit-approve wire proposal, POST accept → 200 and marker applied; a near-match rule → 422. Mutation: drop the kwarg and the positive case goes red |
| E2 | Include `_SECRET_MUTATION_TOOLS` in the explicit-approve preview so a refused wire returns its refusal instead of becoming a proposal | [tool_batch.py L2057](../../../../src/elspeth/web/composer/tool_batch.py#L2057) | Closes N1's proposal half; matches auto-commit outcomes | Explicit-approve turn with an unauthorized wire records no proposal and returns `unauthorized_secret_ref`; an authorized wire still yields `APPROVAL_REQUIRED` |
| E3 | Wire-specific proposal summary ("Wire secret reference NAME into <target> <id> option <key>") and a `ProposalDiff` projector | [proposals.py L122](../../../../src/elspeth/web/composer/proposals.py#L122); [ProposalDiff.tsx L557](../../../../src/elspeth/web/frontend/src/components/chat/ProposalDiff.tsx#L557) | The approver of a credential destination currently sees a generic card | Summary test names reference, target and option and contains no value; frontend test renders a before/after option entry |
| E4 | Report a replaced marker: e.g. `data={"replaced_secret_ref": "OTHER_REF"}` or `"replaced": "literal"` without the value | [secrets.py L467](../../../../src/elspeth/web/composer/tools/secrets.py#L467) (and L404, L514) | N2: the model and user learn that a reference changed | Overwriting `OTHER_REF` reports it; a fresh wire reports nothing; no literal value ever appears |
| E5 | `min_length=1` on `name`/`option_key`; apply `validate_secret_name` to `name`; refuse `.` in `option_key` (or document it as literal) | [secrets.py L92](../../../../src/elspeth/web/composer/tools/secrets.py#L92) | N4, N6: shape errors become `ARG_ERROR` | `""` and `"auth.api_key"` raise `ToolArgumentError`; existing authorized cases pass |
| E6 | Accept the prefixed spelling (`source:<name>`, `node:<id>`, `output:<name>`) as `target_id`, or state "bare name" in the description | [secrets.py L381](../../../../src/elspeth/web/composer/tools/secrets.py#L381), [L555](../../../../src/elspeth/web/composer/tools/secrets.py#L555) | N5 round-trip | Echoing `affected_nodes`/`rejected_component` back as `target_id` resolves the same component |
| E7 | Description: name the full tuple including plugin; add the multi-source rule, bare-name rule, node → `transform` mapping, and "availability is not authorization" | [secrets.py L541](../../../../src/elspeth/web/composer/tools/secrets.py#L541) | G2, G3 | Declaration text updated with the redaction/wire snapshots; teaching gates pass |
| E8 | Progress labels: give `wire_secret_ref` the mutation wording | [progress.py L638](../../../../src/elspeth/web/composer/progress.py#L638) | G4 | Started-event test for `wire_secret_ref` reports a pipeline update, not a check |
| E9 | Skill paragraph (capabilities core): deny-by-default exact destination, terminal `unauthorized_secret_ref`, inline path checked later at preflight | [`pipeline_capabilities.md` L88](../../../../src/elspeth/web/composer/skills/pipeline_capabilities.md#L88) | G1, G5 | Skill-inventory and teaching checks pass; text matches the handler |
| E10 | Derive the batch-dependency source for omitted `target_id` from the state's single source, and add no default for node/output | [batch_dependencies.py L115](../../../../src/elspeth/web/composer/batch_dependencies.py#L115) | N7 | Unit test: a refused `set_source` named `source` does not block a wire on single source `orders` |

## 7. Evidence, gaps, and caveats

Read [authorized wiring assertions](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L386), [near-match rejection controls](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L413), [authorization-before-endpoint tests](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L440), [review drift refusal](../../../../tests/unit/web/composer/test_secret_rejection_contracts.py#L491), and [collector policy tests](../../../../tests/unit/web/composer/test_secret_wiring_collector.py#L68). Included in the seven-file focused run: exit 0, **448 passed, 43 warnings, 66.95s**. Execution gates were source-traced, not exercised against a live deployment; no real secrets, cloud service, PostgreSQL, or browser acceptance was used. Complete alternate-entry parity remains an acceptance requirement, not an inferred blanket success.

Review-pass evidence: in-memory `execute_tool(..., validate_arguments=True)` runs over the `_state()` fixture from `test_secret_rejection_contracts.py` demonstrated N1 (handler consequence), N2, N3, N4, N5, N6 and the identical-repeat version advance; `decode_wire_arguments` runs measured the dialect null semantics; the strict and `none` wire schemas were printed from `composer_loop_tool_definitions`. N1's route omission is source-established only: the accept route was not run, and no PostgreSQL, browser or service acceptance was used. The review pass ran no tests.

## Runtime argument schema and measured exposure

Synchronous kind: `secret_mutation`. Declaration cache opt-in: `false`. Handler: [`elspeth.web.composer.tools.secrets._execute_wire_secret_ref`](../../../../src/elspeth/web/composer/tools/secrets.py#L323).

| Surface | Exposed |
|---|---|
| Canonical emitter | yes |
| Freeform loop | yes |
| Planner default | no |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the runtime-emitted canonical input schema, before provider wire projection. Public dispatch closes the root object where necessary; owned argument models and kind-specific/domain guards can impose additional constraints described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "Secret reference name."
    },
    "target": {
      "type": "string",
      "enum": [
        "source",
        "node",
        "output"
      ],
      "description": "Which component to wire the secret into."
    },
    "target_id": {
      "type": "string",
      "description": "Node ID or output name (required for node/output targets)."
    },
    "option_key": {
      "type": "string",
      "description": "Config option key to set (e.g. 'api_key')."
    }
  },
  "required": [
    "name",
    "target",
    "option_key"
  ],
  "additionalProperties": false
}
```
