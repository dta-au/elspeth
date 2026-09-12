# Bounded implementation charter: aggregation count applicability

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Scratch preparation only. Based on [alignment](count-applicability-implementation-charter.md) and [audit-preserving ordering](expected-output-count-audit-ordering.md), with source-only confirmation of existing OutputMode and adapter structure. Symbol names below identify scope; line numbers in the earlier reports are historical evidence and must not be treated as current mutable coordinates. No application imports, gates, provider calls or repository edits were performed.

## Separate this work from the active MODEL patch

The current MODEL implementation owns scalar/transport strictness and its associated teaching/fixtures. This later patch adds only a cross-field domain applicability rule AFTER structural admission. Do not incorporate the rule into the common node Pydantic model, its subclasses, or any model reused by argument redaction. Wait for the MODEL integration, inspect its resulting owned scalar types, and adapt the call sites without undoing its strict scalar decisions. Add no numeric range/coercion restriction here.

## One smallest pure authority

Use the existing `OutputMode` enum in `src/elspeth/contracts/enums.py`; add a small pure instance method such as `expected_output_count_error(self, count: int | None) -> str | None`. It returns the actionable message only for PASSTHROUGH with non-null count, otherwise None. No new protocol, node model, validator registry, web dependency or configuration wrapper is needed. This keeps core and Composer depending on the same existing contracts type, without core importing web.

Message: `expected_output_count requires output_mode='transform' (the default); omit expected_output_count for 'passthrough'.`

Callers first apply their existing mode admission policy. Runtime already owns OutputMode. Composer maps an admitted omitted/null mode to OutputMode.TRANSFORM for the check only; do not write the default into authored arguments or state. Preserve the existing invalid-mode diagnostic for an unknown mode rather than converting it into this count diagnostic or letting enum conversion crash. Structural mode admission remains the MODEL patch's responsibility where it is already enforced.

One new semantic error code: `aggregation_expected_output_count_mode_invalid`. Use it on Composer semantic failures and state validation. Teaching depends on the small direct-code catalogue/legacy-regex freeze follow-on authorized by `elspeth-d83095ee87` in [remaining residue handoff](remaining-residue-handoff.md). Implement/integrate that follow-on first, then register this new code in its resulting direct code-keyed authority. The current `explain_validation_code` still walks the legacy regex table: calling that existing entry point alone does not meet this requirement. Do not add a new regex row, duplicate catalogue, or separate validator inventory. Extend the established field-to-validator coverage in `src/elspeth/web/composer/tools/schema_contract.py` using the same resulting authority. Teach selecting transform only when that is the intended batch behavior, or removing the count for passthrough. Neither edit is an automatic equivalent repair.

The existing OutputMode.PASSTHROUGH docstring says rows are unchanged; align that description with established executor behavior while touching it: successful passthrough can enrich data while preserving corresponding input identities. This is documentation correction only.

## Exact bounded production scope

- `src/elspeth/contracts/enums.py`: pure applicability method and accurate mode description.
- `src/elspeth/core/config.py`, AggregationSettings: after-model validation invokes that method; raise ValueError using its message. Keep transform default and current runtime null-mode rejection.
- `src/elspeth/web/composer/tools/transforms.py`, _execute_upsert_node: invoke after owned structural admission and before option/plugin processing or mutation. Ordinary semantic failure returns original state/version and code/component.
- `src/elspeth/web/composer/tools/sessions.py`, _build_set_pipeline_candidate: immediately after structural/source-shape admission, sweep owned aggregation arguments before any source blob resolution/preparation. Collect invalid-node diagnostics in input order with the existing bounded component-rejection collector and return the collected failure before custody. Keep subsequent source/node/output processing order unchanged. This stage reports only intrinsic checks it actually performed; it does not claim later source/plugin validation ran.
- `src/elspeth/web/composer/state.py`, aggregation validation branch: same applicability method yields the new diagnostic. Do not change NodeSpec hydration, serialization or stored values; invalid historical/internal state remains inspectable and repairable.
- `src/elspeth/web/composer/yaml_importer.py`, aggregation admission: after existing scalar parsing and before NodeSpec append, invoke the rule, wrap as RuntimeYamlImportError with node/field context. Runtime AggregationSettings alone does not cover this path.
- `src/elspeth/web/composer/yaml_generator.py`, _lower_aggregation_nodes: reject the invalid pair as PipelineLoweringError before emission. Preserve optional-key omission and avoid requiring whole-pipeline completeness to enforce this local invariant.
- `src/elspeth/web/composer/tools/generation.py` and `src/elspeth/web/composer/tools/schema_contract.py`: reuse the direct code-keyed teaching authority introduced by the prerequisite `elspeth-d83095ee87` catalogue/freeze follow-on and the established field coverage, plus the corresponding authoritative aggregation parameter descriptions/fixtures identified by the completed MODEL patch. No new regex row, duplicate catalogue, or separate validator inventory.

`src/elspeth/web/composer/redaction.py` and `audit_storage.py` are verification surfaces, not planned production edits. Full-node argument redaction must continue admitting and showing this structurally valid attempted pair. Semantic failure must not turn into ARG_ERROR: that path deliberately emits only an invalid-arguments summary and loses mode/count. Keep options/blob content redacted; no fallback exposing raw arguments. If MODEL work changes these seams, resolve the integration before implementing applicability.

## Behavioral obligations and bounded tests

1. Pure authority: transform with any structurally admitted integer count passes; passthrough with omitted/null count passes; passthrough with a non-null count fails. Do not test invented numeric restrictions.
2. Real AggregationSettings/settings loading: same result, existing default unchanged, explicit runtime null mode still rejected under its existing contract.
3. Real upsert_node/set_pipeline dispatch: invalid pair returns actionable semantic error, unchanged state/version, and actual redacted attempted arguments retain supplied mode/count. Include secret-like options and inline content to prove sensitive substitutions survive.
4. Full-pipeline inline-blob case: verify preparation/resolution/persistence is never called, not merely that no final blob remains. Multiple bad aggregation nodes exercise ordered bounded diagnostics and existing withheld-count reporting.
5. Composer omitted or explicit-null mode with count uses default transform without rewriting presence. Valid default-transform/count exports with mode omitted where originally omitted, and round-trips through YAML with the same effective behavior.
6. YAML import rejects before replacing state; ordinary/public export lowering rejects invalid internal NodeSpec. Historical hydration stays readable, state validation returns the code, and repairing mode/count succeeds without an automatic rewrite.
7. Executor regression only: transform exact-count mismatch still fails. Passthrough's existing N-or-zero result rule stays independent of expected_output_count: N outputs preserve associated input identities (data may be enriched), a nonzero result of the wrong cardinality fails, and zero outputs settle/discard inputs under current behavior. No executor implementation change or new count semantics.
8. Direct code-keyed teaching/field-validator coverage and actual producer fixture tests include the new actionable diagnostic. Verify its guidance resolves without legacy regex lookup and that the prerequisite legacy-table freeze/ratchet remains unchanged by this patch. Run proportional existing config, Composer tool/audit, YAML, state-validation and aggregation-executor tests after integration; broader repository-required gates belong to the parent integration run.

## Explicit limits

No provider call, deployment, live run, runtime operation, database reset/migration, historical rewriting, tutorial-special path or provider bypass. No new transport constraint, numeric policy, executor behavior, parallel validator inventory or audit exemption. The above paths and their direct tests/authoritative descriptions are the bounded later execution scope; unexpected additional production dependencies must be explained before widening it.
