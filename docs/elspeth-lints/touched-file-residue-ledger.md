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

## Entry 3: Release sync review of execution validation

- Date: 2026-08-01
- Release authority: `origin/release/0.7.2@80c1a85d1`
- Discovered by: merge-conflict resolution and full touched-file review
- Corrected merge: `1f446465b`
- Result: 2 corrected sites in the existing test-double/type-contract drift
  pattern class

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Test-double/type-contract drift | 2 | `_runtime_bundle_double` used `SimpleNamespace` plus `Any`; `_RuntimeGraphDouble` was a bespoke unspecced carrier returning `Any` in `test_gate_fan_out_advisory.py` | Existing test-double/carrier rule: reject `SimpleNamespace`, bespoke structural carriers, and `Any` where an autospecced owned type is available |

Both sites arrived from the release side of the merge and were replaced with
autospecced owned `PluginBundle` and `ExecutionGraph` instances while porting
the release advisory into the refactored validation pipeline. No new recurring
pattern class was required.

Unresolved stale residue for this entry: **0 sites**.

## Entry 4: Completion-gate and trust-regression review findings

- Date: 2026-08-01
- Review anchor: `f4c5b640d`
- Corrected commit: `45ea4e541`
- Discovered by: read-only post-integration review
- Result: 2 corrected sites across 2 new recurring pattern classes

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Terminal-record merge non-idempotence | 1 | `merge_completion_gates` appended a second `advisor_signoff` after `ValidationLedger.finish_failure` had already emitted the canonical skipped terminal slot; repeated merges also accumulated checks and blockers | Replay durable-fact merges over successful, failed-ledger, and already-merged results; assert canonical rank, per-check cardinality, blocker cardinality, and idempotence |
| Trust regression path/suppression blindness | 1 | `test_validation_trust_tier.py` discarded suppression observations, while the synthetic named-exemption regression tested `_collect_secret_refs` under `validation.py` and `_validation_diagnostics.py` instead of its owner `_validation_authoring.py` | Require trust tests to bind each named helper to its production file and snapshot both active findings and suppression observations; mutation-test closed-list exemptions at the real path |

The completion-gate parser's five R1 sentinel probes and two R5 Mapping shape
checks remain explicit, regression-pinned release-signing candidates. They are
well-defined fail-closed parsing obligations, not unresolved stale residue; no
allowlist, signature, staging bundle, or trust-tier rule was changed.

Unresolved stale residue for this entry: **0 sites**.

## Entry 5: Second release sync — static-prompt advisory port

- Date: 2026-08-01
- Release authority: `origin/release/0.7.2@931637f0d`
- Discovered by: merge-conflict resolution, independent port-plan review, and
  full touched-file review
- Merges: `b2e074dc4`, `6dda8c447`; corrected commit: `57194b07b`
- Result: 11 corrected sites across 2 existing and 1 new pattern classes

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Stale coupling to private aliases | 8 | Release's `test_static_llm_prompt_advisory.py` imported `_CHECK_STATIC_LLM_PROMPT_ADVISORY` from the facade (1 import + 7 usages), forcing recreation of an Entry-1 dead alias; migrated to the `schemas` constant | Existing candidate: AST ban on importing facade `_CHECK_*` aliases |
| Test-double/type-contract drift | 2 | `_runtime_bundle_double` (`SimpleNamespace` + `Any`) and bespoke `_RuntimeGraphDouble` — the same two shapes Entry 3 corrected in the sibling gate test | Existing test-double/carrier rule |
| Boundary suppression narrower than body obligations | 1 | `_find_static_llm_prompt_advisories` declared `suppresses=("R1",)` while its `isinstance(template, str)` Tier-3 skip guard is the same honest-parsing R5 shape the sibling identity finder suppresses; on release the resulting unsuppressed R5 was invisible because no per-file conformance pin existed there | Compare each `observation_boundary.suppresses` against scanner observations for the decorated body; flag observed-but-undeclared rules |

The port itself (finder into `_validation_diagnostics.py`, builder into
`_validation_runtime.py`, facade emission loop appended after the gate
fan-out loop to keep check ranks monotone for completion-gate reconciliation)
is intentional feature work, not residue. The three stale
`_reframe_settings_missing_parts` signed allowlist entries keyed to
`web/execution/validation.py` remain untouched release-end stale-delete
obligations. `_CHECK_GATE_FAN_OUT_ADVISORY` remains the one surviving facade
alias, pending its own test migration; tracked separately rather than folded
into this port.

Unresolved stale residue for this entry: **0 sites**.

## Entry 6: Correction to Entry 3 + post-merge dedup sweep

- Date: 2026-08-02
- Discovered by: adversarial verification pass of the merge review (its own
  named detectors, run against Entry 3's reviewed file)
- Result: 6 corrected sites across 2 existing pattern classes

Entry 3 declared zero unresolved residue while its reviewed file still
imported the zero-production-reference facade alias
`_CHECK_GATE_FAN_OUT_ADVISORY` — both of the ledger's own named detectors
("Dead facade aliases", "Stale coupling to private aliases") cover it. Per
the counting rules the published entry stays as written; this correction
records the miss.

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Dead facade aliases | 1 | `_CHECK_GATE_FAN_OUT_ADVISORY` in `validation.py` — the 14th and last alias, surviving only for its test import | Existing zero-reference rule |
| Stale coupling to private aliases | 5 | 1 import + 4 usages in `test_gate_fan_out_advisory.py`; migrated to `schemas.CHECK_GATE_FAN_OUT_ADVISORY` exactly as the identity and static-prompt tests were | Existing AST ban candidate |

The same sweep single-sourced the forked helpers the merge review catalogued
(`_blocked_readiness` ×4 → `_validation_model`, `_snapshot_materialized_evidence`
×2 → `_validation_model`, `_EdgePatchTargetResolver` ×2 → `_validation_diagnostics`),
deleted the dead-but-tested `_append_skipped_checks` seam, made the trust-tier
pin's production-file scope glob-derived, and removed the tautological second
assertion from the signature-stability test — intentional consolidation, not
counted as residue sites.

Unresolved stale residue for this entry: **0 sites**.

## Entry 7: Web Sessions and Composer recursive trust cleanup

- Date: 2026-08-02
- Review anchor: `442fa3393`
- Scope: `src/elspeth/web/sessions/**` and `src/elspeth/web/composer/**`
- Discovered by: recursive trust-tier cleanup and independent full-file review
- Result: 34 corrected sites across 2 existing and 1 new recurring pattern classes

| Pattern | Sites | Exact surface | Plausible detector |
|---|---:|---|---|
| Signature-churn manipulation | 20 | Three dead import pins, five signed-layout local-import blocks, artificial `post_guided_convert` placement, and two false-green AST/order tests in the guided route surface (11); four placement/EOF comment blocks plus the locally pinned Jinja import in `composer/service.py` (5); placement/fingerprint comments in `composer/audit.py`, `composer/tools/generation.py`, `sessions/routes/_helpers.py`, and `sessions/routes/guided_operations.py` (4) | Reject comments and `noqa` rationales containing signature/fingerprint/AST-position vocabulary; flag local imports without a demonstrated cycle; mutation-test route-order assertions by moving the handler to its logical lifecycle position |
| Dead compatibility and stale coupling | 9 | Redundant local UUID import in `tutorial_service.py` (1); dead `PIPELINE_COMPOSER_SKILL_FILENAME` and `PIPELINE_COMPOSER_SKILL_HASH` exports plus three tests coupled to the cached compatibility hash (5); dead `state_machine.SinkResolved` re-export and its fixture import (2); `tools/blobs.py` comment describing a nonexistent cleanup hook (1) | Zero-reference private/export graph with an explicit compatibility manifest; forbid cached-content-hash assertions when the authoritative content can be hashed directly; documented-symbol existence check over touched comments |
| Documentation/guidance drift | 5 | `pipeline_composer.md` twice required callers to surface backend-owned `llm_prompt_template` rows, repeated that contradiction in the requirement matrix and termination checklist, and documented a five-field preflight object although the authored tool contract accepts exactly three fields | Cross-check skill prose and tables against tool JSON schemas and ownership metadata; require repeated requirement matrices/checklists to agree on the same owner and payload shape |

The recursive review also corrected intentional trust-boundary and correctness
defects; those behavior changes are excluded from the residue count. Honest
Tier-3 vendor parsing findings, inherited signed drift, and proven stale
allowlist entries remain separate key-free adjudication/signing obligations.
No allowlist, judge signature, staged-review bundle, or signing workflow was
modified.

Unresolved stale residue for this entry: **0 sites**.

## Running totals

| Features reviewed | Corrected sites | Pattern classes recorded | Unresolved residue |
|---:|---:|---:|---:|
| 1 | 100 | 9 | 0 |
| 2 | 101 | 10 | 0 |
| 2 | 103 | 10 | 0 |
| 2 | 105 | 12 | 0 |
| 2 | 116 | 13 | 0 |
| 2 | 122 | 13 | 0 |
| 3 | 156 | 14 | 0 |
