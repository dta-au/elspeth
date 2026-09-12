# Seam 6 integration charter — design only

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

The scorecard is not complete. MODEL/READ/ADMITTED follow-on gates are dependencies, not results to fabricate while their APIs are absent. This charter uses the live plan `docs/plans/2026-09-08-composer-wires-campaign.md` (especially progress corrections and Seam 6), current census source, existing teaching/frontend gates, and `admitted-scorecard-handoff.md`. Source inspected read-only; no gate helper imports or provider calls.

## Keep one small integration point

Extend `scripts/cicd/composer_wire_census.py --scorecard`. Its existing `ModelWireRow`/`census_model_wire` and `TaughtWireRow`/`census_taught_wire` are the starting point. Gate tests should assert over the same census results the command renders. Do not import test modules to execute tests, copy their extraction logic into a parallel renderer, or build a generic reporting framework.

Join every per-wire mapping onto the current `get_tool_definitions()` universe after duplicate-name and exact-set checks. Keep existing handler/interception reconciliation in `census_model_wire:216-230`. No union-of-available-rows join: it hides missing endpoints. No outer join followed by default “N/A.” Each census must account for each tool or return a concrete unresolved failure. Counts come from returned sets, never a fixed total.

Use tool name plus structured source path/qualname as current evidence anchors; no new sidecar receipts, signing or manually maintained row manifests. `--scorecard` is a source-derived report, not proof a test run passed. A CLI extraction failure should exit nonzero with the missing source/tool named. A complete extraction can still report uncovered/mismatched cells; it must not call them green.

## Concrete census result contracts needed

These are additions to the existing small wire-specific records, not a common generic cell hierarchy.

| Census | Facts the record must return | Meaning the renderer may claim |
|---|---|---|
| SHIPPED | tool; source definition location; root property-name set; schema root/ref evidence | Number of advertised root keys, not recursive semantic field count. Closed root or an open option bag is separate schema evidence. |
| MODEL | existing shipped/model identity/site; resolved input-carrier status; accepted root wire names; unsupported alias/refinement evidence; relation results from existing schema-contract authority | “Extracted model, root keys matched” is weaker than “checked supported schema semantics.” Current `model_fields` holds Python names, so alias-aware follow-on work must expose accepted wire names explicitly. `None` model is not automatically N/A. |
| READ | handler/wrapper source path; root argument or validated-object identity; direct reads; delegated/carried fields and callee evidence; unresolved consumers; declared remaining bag boundaries | “Statically attributed” proves provenance/read extraction, not that a runtime branch uses the value or alters the intended behavior. Whole-object forwarding is a distinct carried/delegated status, not a fake read of every field. Behavioral pins remain separately named. |
| ADMITTED | manifest entry source; existing mode `closed_declarative`, `type_driven`, or `open_declarative`; known-key set including empty sets; model identity/accepted wire names when applicable; emitted-name/alias relation or explicit unsupported case | Closed allowlist equality or type-model root admission. Open declarative is a real existing policy category, not missing policy and not an admission pass. Sensitive value treatment is a separate property, not implied by name preservation. |
| TAUGHT | existing shipped/taught/declared sets; own-context source ownership; unresolved/stale keys | Lexical own-context coverage only. Existing `test_tool_knob_teaching_gate.py:1-5` expressly disclaims semantic quality and authorizes no argument fences. Keep that disclaimer in output. |
| FRONTEND | producer registry membership; dispatch-supported membership from existing frontend guard evidence; generated fixture membership/cases; whether live MANIFEST summarizes an argument; applicable comparison result | “Projected, summarized fixture covered” versus “projected, no summarized argument” versus “not projected by current frontend registry.” The Python fixture's inventory alone cannot prove live frontend dispatch parity. |

Expose gate relation facts as explicit sets/differences and named capability boundaries, not a mutable `passed=True` flag computed independently by CLI. Use existing `CensusError` for missing source/unsupported extraction where no valid result can be produced. Existing `site="unresolved:..."` is a diagnostic that must stay visible until MODEL follow-on resolves it; do not convert it to an empty model match.

## Derived applicability, not disguised unknowns

- A zero-key tool has derived SHIPPED count zero. READ can say “no root argument fields to read” only after handler/interception ownership resolved and the registry actually advertises no arguments. Missing handler source is unresolved even if keys are zero.
- A handler with no input model is a pending owned-admission repair even if manual ownership is resolved. Universal original-input owned admission is required for every shipped tool, including empty-input tools and advisor interception. Missing model or unresolved advisor consumer is never final MODEL N/A.
- A closed empty allowlist is applicable and evaluated as empty-set equality. It must remain in ADMITTED's comparison universe so a newly advertised key produces a failure.
- An open declarative manifest entry is “existing open policy; pending authorized tightening.” Do not imply its arguments never persist. Our direct redactor probes show passthrough, but did not establish complete dispatch/persistence behavior. Internal policy tightening is authorized; only a concrete externally imposed constraint with source evidence can justify a resolved exception.
- FRONTEND not-projected must be derived from the generated runtime registry plus its independent dispatch-coverage guard. “Not in fixture” alone is insufficient: that also describes an accidentally removed case. Projected-but-not-summarized is justified by `walk_model_schema` Sensitive metadata or declarative sensitive_argument_keys, matching the existing Python fixture gate.
- A missing required fixture or generated inventory is an extraction error. A missing frontend verification run leaves live dispatch evidence unverified; the scorecard can still name the extracted registry membership without claiming a behavioral pass.

## Frontend and test-run boundary

Existing Python fixture tests load the generated projected-tool inventory (`test_proposal_diff_redaction_fixture.py:81-115`), compare it to MANIFEST and pin production redactor bytes. Vitest owns inventory-to-runtime-dispatch coverage; Python CI must not acquire a Node dependency merely to render the scorecard. Reuse the existing generated artifact and show the boundary explicitly. A separate recorded test invocation can establish guard success; absent that evidence, label the source relationship as extracted, not verified by execution. The scorecard should not manufacture proof-of-test metadata.

Likewise, MODEL root-key extraction does not prove enum/pattern/required/null/nested semantics; ADMITTED extraction does not prove value redaction; READ extraction does not prove behavior; TAUGHT lexical coverage does not prove comprehensibility. A compact table may give the relation label and missing-key count with detailed tool diagnostics below, but must preserve these distinctions.

## Integration mutants worth running

1. Add a real registered tool definition/handler in isolated test data while omitting exactly one wire's row: exact tool-universe reconciliation must fail and name that wire. Do not mutate only a renderer table.
2. Remove a source file/registry endpoint needed by an existing tool: extraction fails, never emits N/A or an empty matching set. Remove teaching helper/fixture independently to test their actual loading boundaries.
3. Add a shipped key to a closed-empty tool without an allowlist change: ADMITTED must fail even though other allowlists remain populated.
4. Add an input alias to a synthetic model and advertise the Python name instead: accepted-wire-name comparison must fail or explicitly report unsupported alias form. A root match must not mask a semantic enum/required mismatch supported by the schema-contract gate.
5. Stop consuming a shipped field in a representative handler while preserving MODEL/SHIPPED declarations: READ must report lost attribution. Forward the object to an unresolved helper and prove it does not mark all fields consumed.
6. Remove a projected tool from both exported inventory and fixture but retain frontend dispatch: existing Vitest guard must fail, as already demonstrated in the plan. Python membership alone is not the mutant oracle.
7. Remove an own-context argument description while leaving the same word elsewhere: TAUGHT must fail its existing ownership check rather than borrow unrelated prose.

These are mutations of the actual relation sources and should run against the same bounded selection before mutation, during mutation and after restoration. No generic mutant runner is required.

## Explicit implementation dependencies

Before scorecard integration can be called finished: MODEL follow-on proves original-input owned admission for every shipped tool and exposes supported-semantic coverage; READ exposes direct/delegated/unresolved provenance; ADMITTED closes the empty-list omission and measures the authorized policy tightening; frontend existing registry/fixture guard remains authoritative; TAUGHT source ownership remains unchanged. Remaining current open policies and unresolved MODEL/READ paths are pending repairs, not optional choices or resolved exceptions absent concrete externally imposed justification. A test that merely forbids the literal word `unknown` is insufficient and should instead require complete derived accounting with no unresolved cell disguised as applicable success.

The eventual live battery trial is separate behavioral evidence required by Seam 6. This design does not satisfy it, run it, or claim the scorecard complete.

## Superseding operator ruling: universal owned admission

The latest user ruling, recorded in `argument-model-ruling-amendment.md` and the final parent-edited `model-admission-implementation-charter.md`, supersedes the earlier manual-boundary and optional-open-policy treatments in this charter. Every shipped tool, including empty-input tools and advisor interception, must have proven original-input owned admission. A currently manual/no-model boundary is an observed pending repair or extraction failure, never a legitimate final MODEL N/A. Open policy tightening is authorized internal work; remaining current openness is evidence of unfinished work, not an optional operator choice. Only a concrete externally imposed constraint can justify an exception, with its source and preserved boundary stated.

Scorecard admission/error evidence must distinguish malformed input from a structurally valid semantic invalid pair. Malformed model validation follows safe ToolArgumentError/ARG_ERROR handling: unchanged state/version, no advisor budget/provider effects, bounded classification and field-count audit evidence. This deliberately does not preserve raw supplied values. Structurally valid but inapplicable mode/count combinations use an ordinary failed ToolResult and retain their permitted redacted values. A scorecard must not mark loss of malformed supplied values as a redaction defect or claim semantic-pair evidence from a malformed-input test.
