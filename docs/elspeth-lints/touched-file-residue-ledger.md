# Touched-File Residue Ledger

This ledger records stale implementation residue that a feature's first cleanup
missed and a later review corrected. Its purpose is to expose recurring patterns
that are worth automating, not to score changes or create release ceremony.

## Counting rules

- Count one site per source location, private symbol, compatibility obligation,
  or stale plan checkbox. Do not count changed lines.
- Include only residue present after the first cleanup and corrected by a later
  review of the same feature.
- Exclude intentional feature implementation and deliberate behavior changes.
- Keep published feature entries immutable. If a count needs correction, append
  a correction entry rather than rewriting the original observation.
- Track unresolved residue separately; do not include it in corrected totals.
- Keep known adjudication and release-signing obligations separate from residue.

## Entry 1: Execution validation pipeline refactor

- Date: 2026-08-01
- Plan: [Execution Validation Pipeline Refactor Implementation Plan](../superpowers/plans/2026-08-01-execution-validation-pipeline-refactor.md)
- Tracker: `elspeth-39d6d479c0`
- Review baseline: `1a2fda249`
- Corrected commit: `e554e491b`
- Result: 100 corrected sites across 9 recurring pattern classes

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Compatibility surface omission | 5 | `_validation_diagnostics._infer_component_type_from_plugin_error`; facade re-export in `validation.py`; export-identity regression; behavior regression; trust-candidate inventory | Explicit compatibility-export manifest tested for import identity and behavior |
| Production `Any`/type erasure | 3 | `_YamlLoader.__call__`; `materialize_validation_yaml(load_yaml=...)`; `cast(dict[str, Any], ...)` | AST rule forbidding `Any` at validation phase boundaries except reviewed allowlist |
| Dead facade aliases | 13 | `_CHECK_PLUGIN_ENABLEMENT` through `_CHECK_IDENTITY_NODE_ADVISORY` in `validation.py` | Reference-graph/Vulture rule for zero-reference private assignments, with compatibility-manifest exemptions |
| Stale coupling to private aliases | 6 | Import, assertion, and three filters in `test_identity_node_advisory.py`; one integration-test source-pointer comment | AST ban on importing facade `_CHECK_*` aliases plus stale-symbol comment scan |
| Dead duplicate wrappers | 2 | `_format_edge_contract_failure` and `_build_edge_contract_suggestion` in `_validation_diagnostics.py` | Private-function zero-caller detector |
| Lost load-bearing rationale | 1 | Diagnostic convergence rationale had been attached to deleted wrapper; now retained on `_build_edge_contract_suggestion_with_resolver` | Review check requiring deleted substantial docstrings to preserve designated rationale anchors near replacement symbols |
| Stale source documentation | 1 | `ValidationLedger.checks` still described a “not-yet-extracted legacy tail” | Banned/stale vocabulary scan over touched files |
| Test-double/type-contract drift | 17 | `_loaded`, `_bundle`, `_instantiated`, `_autospec_callable`, `_unexpected_edge_target`, `_graphed`, `_snapshot`, and four runtime phase tests in `test_validation_runtime.py` | AST checks for `cast(Any, ...)`, concrete carriers built from `SimpleNamespace`/`object()`, and always-raising callbacks returning `Any`; require autospecced owned types |
| Documentation/guidance drift | 52 | `AGENTS.md` scope and judge transport: 2; plan status/deviations plus 40 completed-but-unchecked steps: 44; design status/context/deviations/stale `ValidationRequest`: 6 | Plan checkbox/status consistency check, documented-symbol existence check, and CLI-option validation against authoritative workflow docs |

### Highest-value detector candidates

1. Compatibility export manifest: declare compatibility seams and verify both
   import identity and behavior.
2. `Any` at validation boundaries: an AST rule for boundary callbacks, carrier
   fields, admitted mappings, and casts.
3. Zero-reference private symbol graph: flag dead assignments and functions,
   with explicit compatibility-manifest exemptions.
4. Test double/carrier rule: reject `SimpleNamespace`, `object()`, or
   `cast(Any, ...)` where a concrete owned carrier or autospec is available.
5. Documentation drift: compare plan status and checkboxes, validate documented
   symbols, and check CLI spellings against the authoritative workflow.

### Unresolved and excluded obligations

Unresolved stale residue for this entry: **0 sites**.

The following known obligations are excluded from the corrected-residue total:

- Three explicit nominal R5 adjudication candidates:
  `_validation_authoring._secret_ref_exists`,
  `_validation_authoring.review_interpretations`, and
  `_validation_diagnostics._infer_component_type_from_plugin_error`.
- Three signed `_reframe_settings_missing_parts` entries that remain release-end
  stale-delete obligations. No allowlist or signature was changed by this
  cleanup.

## Entry 2: Coalesce property reachable-state generator

- Date: 2026-08-01
- Tracker: `elspeth-b81363eef7`
- Discovered by: CI-equivalent full-suite verification
- Corrected commit: `affa74935`
- Integration commit: `6ed97d5af`
- Result: 1 corrected site in 1 new recurring pattern class

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Filter-heavy property generator | 1 | `test_maybe_coalesce_matches_legacy_step_semantics_for_reachable_states` drew three ordered step values independently, then discarded unreachable combinations with two `assume()` calls | AST rule for `@given` parameters compared to one another in `assume()`; require a dependent strategy when the constraint defines the intended input domain |

The reproducing seed generated only 9 valid inputs while discarding 50. The
replacement strategy directly generates
`1 <= current_step <= coalesce_step <= step_count <= 10`, preserving all
property assertions and the 250-example budget. Full-file review found no
additional stale residue or trust-tier obligation.

Unresolved stale residue for this entry: **0 sites**.

## Running totals

| Features reviewed | Corrected sites | Pattern classes recorded | Unresolved residue |
|---:|---:|---:|---:|
| 1 | 100 | 9 | 0 |
| 1 | 101 | 10 | 0 |
