# ADMITTED scorecard handoff

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.
> The final pending-policy decision below is superseded: tightening internally owned open policies is authorized. Preserve separate disclosure policy; no new argument fence is approved.

Read-only runtime registry/MANIFEST census at HEAD 1278c5c21. Imports used LITELLM_LOCAL_MODEL_COST_MAP=True (supported at plugins/llm/model_catalog.py:79-85), both worktree source roots, no provider calls. Existing authority only; no new policies. Full derived records: admitted-live-rows.json beside this report.

## Live per-tool ledger

SHIPPED is get_tool_definitions()[].parameters.properties. ADMITTED source is MANIFEST[tool].policy.known_argument_keys for declarative tools or MANIFEST[tool].argument_model for type-driven tools. “Open declaration” is not missing MANIFEST policy: these entries have policy objects with no known-key closure.

| Tool | Existing admission mode | Shipped keys | Authority / current gate disposition |
|---|---|---|---|
| list_blobs | closed-declarative | (none) | policy.known_argument_keys; EMPTY closed allowlist omitted by helper |
| list_composer_blobs | closed-declarative | (none) | policy.known_argument_keys; EMPTY closed allowlist omitted by helper |
| get_blob_metadata | closed-declarative | blob_id | policy.known_argument_keys; currently compared |
| get_blob_content | type-driven | blob_id | GetBlobContentArgumentsModel at src/elspeth/web/composer/redaction.py:3300; excluded by current allowlist helper |
| create_blob | type-driven | filename, mime_type, content, description | CreateBlobArgumentsModel at src/elspeth/web/composer/redaction.py:1675; excluded by current allowlist helper |
| update_blob | type-driven | blob_id, content | UpdateBlobArgumentsModel at src/elspeth/web/composer/redaction.py:1614; excluded by current allowlist helper |
| delete_blob | closed-declarative | blob_id | policy.known_argument_keys; currently compared |
| wire_blob_inline_ref | closed-declarative | field_path, blob_id, encoding | policy.known_argument_keys; currently compared |
| list_sources | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| set_source | type-driven | source_name, plugin, on_success, options, on_validation_failure, description | SetSourceArgumentsModel at src/elspeth/web/composer/redaction.py:1441; excluded by current allowlist helper |
| patch_source_options | type-driven | source_name, patch | PatchSourceOptionsArgumentsModel at src/elspeth/web/composer/redaction.py:2208; excluded by current allowlist helper |
| clear_source | closed-declarative | source_name | policy.known_argument_keys; currently compared |
| set_source_from_blob | type-driven | blob_id, source_name, plugin, on_success, on_validation_failure, options | SetSourceFromBlobArgumentsModel at src/elspeth/web/composer/redaction.py:1526; excluded by current allowlist helper |
| set_source_from_blobs | type-driven | blob_ids, source_name, on_success, on_validation_failure, options | SetSourceFromBlobsArgumentsModel at src/elspeth/web/composer/redaction.py:1594; excluded by current allowlist helper |
| inspect_source | closed-declarative | blob_id | policy.known_argument_keys; currently compared |
| get_pipeline_state | open-declarative | component | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| set_pipeline | type-driven | source, sources, nodes, edges, outputs, metadata | SetPipelineArgumentsModel at src/elspeth/web/composer/redaction.py:2115; excluded by current allowlist helper |
| get_plugin_schema | open-declarative | plugin_type, name | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| get_expression_grammar | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| explain_validation_error | open-declarative | error_text | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| get_plugin_assistance | open-declarative | plugin_type, plugin_name, issue_code | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| get_audit_info | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| list_models | open-declarative | provider, limit | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| preview_pipeline | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| diff_pipeline | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| list_transforms | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| list_sinks | open-declarative | (none) | policy exists, closure disabled; measured passthrough, no ADMITTED comparison |
| upsert_node | closed-declarative | id, node_type, plugin, input, on_success, on_error, options, condition, routes, fork_to, branches, policy, merge, trigger, output_mode, expected_output_count, timeout_seconds, description, scope_name, scope_opener, scope_policy | policy.known_argument_keys; currently compared |
| splice_transform | type-driven | predecessor_id, successor_id, node | SpliceTransformArgumentsModel at src/elspeth/web/composer/redaction.py:4694; excluded by current allowlist helper |
| upsert_edge | closed-declarative | id, from_node, to_node, edge_type, label | policy.known_argument_keys; currently compared |
| remove_node | closed-declarative | id | policy.known_argument_keys; currently compared |
| remove_edge | closed-declarative | id | policy.known_argument_keys; currently compared |
| set_metadata | closed-declarative | patch | policy.known_argument_keys; currently compared |
| patch_node_options | type-driven | node_id, patch | PatchNodeOptionsArgumentsModel at src/elspeth/web/composer/redaction.py:2234; excluded by current allowlist helper |
| set_output | closed-declarative | sink_name, plugin, options, on_write_failure, description | policy.known_argument_keys; currently compared |
| remove_output | closed-declarative | sink_name | policy.known_argument_keys; currently compared |
| patch_output_options | type-driven | sink_name, patch | PatchOutputOptionsArgumentsModel at src/elspeth/web/composer/redaction.py:2270; excluded by current allowlist helper |
| list_secret_refs | closed-declarative | (none) | policy.known_argument_keys; EMPTY closed allowlist omitted by helper |
| validate_secret_ref | closed-declarative | name | policy.known_argument_keys; currently compared |
| request_advisor_hint | closed-declarative | trigger, problem_summary, recent_errors, attempted_actions, schema_excerpt | policy.known_argument_keys; currently compared |
| request_interpretation_review | type-driven | affected_node_id, kind, user_term, llm_draft | _RequestInterpretationReviewRedactionModel at src/elspeth/web/composer/redaction.py:1639; excluded by current allowlist helper |
| wire_secret_ref | closed-declarative | name, target, target_id, option_key | policy.known_argument_keys; currently compared |

## Actual coverage and runtime consequence

Live imports measure 42 registered tools: 18 closed declarative (52 advertised keys), 12 type-driven (43 keys), 12 open declarative (9 keys). The current allowlist test iterates only 15 closed nonempty allowlists; 3 closed empty entries disappear at `_argument_allowlists`' `if keys`, and the final `fail_closed & admitted` intersection hides that loss. The docstring's 15/42 and 52/104 still match the narrow iteration today, but its claim the remaining 27 run open is false: type-driven entries validate and redact through models, and three closed-empty tools are omitted.

**Three omitted but genuinely closed surfaces:** list_blobs, list_composer_blobs, list_secret_refs. They ship no knobs now, so current equality would be empty == empty, not proof the omission is harmless for future changes. If someone advertises a new key while keeping the empty closed allowlist, the current gate silently skips it. Direct `redact_tool_call_arguments` probe with `{'safe_unadvertised_probe':'safe-probe-value'}` emits only `{'_unknown_arguments':'<redacted-unknown-argument-key>'}` for each. This is a concrete existing-policy consequence; a new advertised field would be destroyed by name.

**Type-driven surfaces:** all 12 imported argument models currently have alias, validation_alias and serialization_alias null on all top-level model fields. Their top-level field names match advertised names in the measured rows. This is measured matching shape, not existing parity-test coverage. Production validates via model_validate at redaction.py:2578, walks schema at :2579, and `_redact_via_schema` uses model_dump at :2324; nested Sensitive markers determine value replacement. Existing test_redaction_completeness_property.py:107 proves marked-sensitive values replaced for eligible model examples; it does not prove every advertised name is admitted by the model. Root field schema walker :850-864 keys by model field name. Future aliases need explicit treatment of accepted wire names versus emitted audit names; blindly comparing Python fields is insufficient.

**Five argument-bearing open declarative entries (9 advertised keys):** get_pipeline_state(component), get_plugin_schema(plugin_type,name), explain_validation_error(error_text), get_plugin_assistance(plugin_type,plugin_name,issue_code), list_models(provider,limit). Calling the production redactor directly preserves all supplied advertised keys plus a safe unadvertised probe. This isolates redactor behavior; it is not a claim dispatch accepts those extras or that the supplied synthetic values satisfy handler schemas. Existing-policy score must say OPEN DECLARATIVE / NO KEY CLOSURE, not FAIL or ADMITTED PASS. There are seven other open declarative entries with no advertised arguments; they also are not covered by the current allowlist relation.

Production `_redact_via_policy:2436-2447` shallow-copies arguments and closes keys only when policy_closes_unknown_arguments says so. Constructor ToolRedaction enforces exactly one of model/policy; no currently registered entry lacks both. Do not call these five “no-policy tools” literally; that obscures their standing HandlesNoSensitiveDataReason policy and would justify an unrequested new disclosure rule.

## Bounded extension of the EXISTING gate

Extend `tests/unit/web/composer/test_tool_argument_wire_parity.py`, rather than adding a second manifest/registry inventory:

1. Retain every declarative entry in the helper, including empty known-key tuples. Drive forward checks from the production closed set directly and use explicit indexed registry/manifest membership after set equality, never `shipped.get(tool, empty)` or an intersection that silently narrows the comparison. Assert evaluated-tool set equals the production closed set. Reverse staleness check remains appropriate only for declared known keys.
2. Add a separate type-driven relation, derived from argument_model validation schema and actual accepted wire names. Inspect Pydantic aliases/config; do not conflate validation_alias, alias and serialization_alias. At this HEAD all argument roots have no aliases, permitting a strict first version that diagnoses unsupported alias shapes explicitly rather than silently inventing a mapping. If parent wants alias support, use concrete alias-aware input/output probe with the production model/redactor, not guessed field-name equivalence. Nested bags remain bags; don't pretend top-level equality proves nested coverage.
3. Keep open-declarative category explicitly outside key-closure parity. Derive its rows and reason reference in the scorecard; do not add known keys or redact flags to make the report green. Registry/manifest set parity still covers their existence.
4. Rewrite scope prose using derived categories (or omit hardcoded totals). Ensure MODEL, ADMITTED, value-redaction coverage, READ, and TAUGHT remain distinct measurements. Existing frozen-manifest non-sensitive justification and sensitive-property tests are complementary, not substitutes.

### Mutant probes that should make the gate fail

- **Empty closed keys:** inject a synthetic advertised key into one closed-empty tool's definition without changing MANIFEST. The gate must identify that tool/key; this is precisely the current omission. A neutral empty==empty case must remain evaluated and recorded.
- **Missing catalogue entry:** remove a tool from shipped definitions while retaining MANIFEST; remove the converse entry separately. Set parity and direct indexing must fail loudly, never default to an empty key set. If “catalogue” means missing argument_model field, remove a shipped field from a synthetic type-driven model and require a tool/field mismatch; do not permit model-shape absence to recategorize it as open.
- **Aliases:** synthetic model with an input alias whose accepted wire key differs from Python field name; deliberately advertise the wrong name and prove failure. Cover validation_alias and serialization_alias separately if supported. A mutation to serialize_by_alias can affect output keys even when input validation remains valid; production redactor output must be observed for supported cases. Unsupported alias forms such as AliasChoices/AliasPath must report unsupported evidence rather than pass. Existing production models having no top-level aliases is not a reason to let a future alias bypass the relation.
- **Existing closure predicate:** preserve request_advisor_hint coverage where known keys close policy without redact_unknown flag. Existing predicate-identity test already catches flag-only drift.
- **Missing whole tool:** a fully removed relation endpoint cannot appear as 0 missing fields; it must fail set parity before field comparison.

All mutants should be local test data/monkeypatches restored by fixture scope; no production MANIFEST changes. Runtime value examples must use nonsecret fixture strings, never operator data.

## Decisions still pending

Whether to close the five argument-bearing open declarative surfaces is a disclosure-policy decision outside this measurement. Whether future aliases are supported or explicitly rejected by this narrow gate is an implementation coverage choice; unsupported must remain visible. Historical allowed aliases for twin error carriers are not proposed. No unresolved category is marked pass.

## Confidence Assessment
High on imported live rows, current gate branch omission, and direct runtime redactor probes. Moderate on total nested type-driven coverage: this seat did not run the full redaction property suite or build every valid type-driven argument fixture.

## Risk Assessment
Low for proposed measurement-only gate extension. High if an “ADMITTED” fix silently creates new policies or treats preservation of field names as authority to disclose values. Alias-aware validation and serialization are distinct, so a naive alias gate can provide false confidence.

## Information Gaps
No full pytest invocation; direct read-only probes only. No proof about upstream dispatch rejection or real secret-bearing values for open declarative probes. No exhaustive nested alias analysis. Parent's scorecard must keep these limitations visible.

## Caveats & Required Follow-ups
Re-import live registry after concurrent campaign edits before publishing final counts. Run the extended existing gate and relevant property tests with recorded exit codes, then its mutant probes. Claims about all tools require partition/set reconciliation, not only a nonempty global dictionary assertion.
