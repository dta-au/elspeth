# Composer explicit failure choices

Status: systems/security ordinary-component contract accepted with the public
import clarification incorporated below; independent LLM/correctness design
review remains pending before production or test edits. Tracks GitHub issue
#171. This changes Composer admission, not runtime failure policy or
required-control coverage.

## Problem and bounded decision

Composer currently fills omitted ordinary transform and aggregation `on_error`,
source `on_validation_failure`, and sink `on_write_failure` with `discard`.
The planner's missing decision therefore becomes an executable row-loss choice.
The runtime already requires explicit routes for transforms, aggregations, sinks,
and ordinary source configurations. Gate `on_error` omission instead means
fail-fast; structural barriers have their own failure contracts.

New ordinary planner-authored components must supply their required failure
choice. A partial update that omits that field preserves an existing explicit
choice for the same component identity and kind. A new component with no choice
is rejected with an owned, actionable diagnostic and unchanged state. The server
does not select a route. Explicit `discard` remains legal; named routes remain
subject to existing topology, failsink eligibility and before-write coverage.

This does not introduce safe quarantine before release, weaken mandatory
controls, exempt covered ordinary nodes, or promise that quarantine is legal
where existing coverage forbids it. Policy-owned required-control routes keep
their already approved behavior. Provider ownership and tutorial stimulus remain
unchanged; all tools use the ordinary shared contract.

## Admission semantics

| Surface / value | Required ordinary failure field | Optional gate field |
| --- | --- | --- |
| New component, omitted | Reject with field/component diagnostic | Preserve existing fail-fast omission |
| Partial update, omitted | Preserve prior nonempty choice for same identity and kind; otherwise reject | Preserve prior choice, including intentional omission |
| Explicit `null` | Reject; never preserve or substitute `discard` | Clear to fail-fast, as today |
| Empty or whitespace-only string | Reject | Reject |
| Explicit `discard` | Keep exactly that choice, then existing admission | Keep explicit choice |
| Named route | Keep canonical trimmed route, then existing admission | Keep canonical trimmed route |

Full `set_pipeline` is a replacement, not a partial update: every required
ordinary component supplies a choice even if its identifier existed before.
An options-only patch preserves existing routing. A changed node kind does not
inherit a policy from an unrelated prior kind. The new splice transform always
needs its own choice; an idempotent already-applied splice keeps existing state.
Collector/coalesce/row-union/queue rules remain type-specific; no required route
is invented for structural kinds. Internal resume-only NullSource is not a new
Composer ingestion surface and direct runtime `NullSource({})` behavior is
unchanged. Every currently public Composer source authoring/import path still
requires an explicit nonempty choice for a new source, including `plugin: null`
if that plugin is otherwise admitted. Plugin identity does not grant trusted
resume authority.

For update preservation, identity means the same named source/sink or the same
ordinary node id and kind. A plugin replacement at that same identity/kind may
retain an omitted route, followed by ordinary route and policy validation.
Different component identity cannot borrow a choice merely because the plugin
matches. Empty or stale invalid stored routes are not valid prior choices.

Omission is measured from the original argument mapping or Pydantic
`model_fields_set`, not from `value is None`. The field/value distinction must
survive every handler, required-control projection and redaction reconstruction.
Use the existing `_OmittableString` distinction where appropriate. Audit
`model_dump(exclude_unset=...)` and provider-wire schema lowering: neither a
full dump nor strict-schema normalization may fabricate a supplied field or
turn an omitted update into explicit null. Explicit provider-supplied null must
remain null and be refused for a required ordinary route, never interpreted as
omission. Full replacement cannot preserve old choices through reconstruction.
No compatibility shim, fallback, or legacy persisted-state rewrite is added.
Existing canonical states already contain their resolved required routes;
their provenance is not retroactively relabeled as a new planner decision.

## Owning paths and implementation sequence

1. `src/elspeth/web/composer/tools/_common.py`: replace the source-only
   missing/null/empty-to-discard canonicalizer with one narrowly typed failure
   choice admission helper, used by ordinary tool handlers. It takes the field
   name, whether supplied, parsed value, and optional prior choice and returns
   either a canonical route or an owned fixed diagnostic naming the field and
   component location. Never echo raw Pydantic errors, YAML scalar values,
   options, credentials or arbitrary exception prose. The public YAML route
   renders importer errors into HTTP400, so this privacy rule applies there
   as well. Keep low-level source
   prevalidation's internal probe defaults separate from author admission;
   no executable authored component may reach it without a resolved choice.
2. `tools/transforms.py`: `_execute_upsert_node` resolves the field before
   prevalidation/construction, using prior node identity/kind only on omission;
   remove the `or "discard"` constructor fallback. The splice construction
   path requires the inserted transform's choice. Options-only patch and
   serialization use actual admitted routes, not a second fallback. Optional
   gate semantics remain unchanged.
3. `tools/outputs.py`: remove `_SetOutputArgumentsModel`'s implicit default.
   Retain an omission-aware optional argument shape so an existing sink update
   can preserve its route; new sink omission produces semantic repair feedback.
   Explicit null/empty fails. The sink constructor receives the resolved value.
4. `tools/sources.py`: `set_source`, `set_source_from_blob`, and
   `set_source_from_blobs` resolve explicit/preserved failure routes before
   plugin prevalidation or blob construction. Options-only source patches keep
   the actual prior route. Source identity is the named source key, not plugin
   name alone. Blob MIME inference does not choose failure routing.
5. `tools/sessions.py`: both named and legacy singular source paths, ordinary
   node paths, and sink paths in full `set_pipeline` require supplied routes.
   Collect component diagnostics before construction using the existing
   rejection accumulator; no partially accepted candidate or policy default.
6. `src/elspeth/web/composer/required_controls.py`: ordinary candidate parsing
   must refuse a missing/null/empty required failure route rather than create
   a temporary discard projection. It must not make a malformed candidate look
   covered. A projection returning `None` hands the candidate downstream and
   is not itself a complete rejection: mutation admission must independently
   refuse with unchanged state/version/proposal. No policy autowire may turn
   the missing ordinary route into an executable choice. The policy-owned
   mandatory nodes' explicit discard choices remain.
   Existing before-write postdominator and author-around-error refusal remain.
7. `src/elspeth/web/composer/redaction.py` and tool JSON declarations: preserve
   omission tracking and align nonempty/null semantics with handler admission.
   Full-state source/sink required fields are advertised as required. Upsert
   and source/sink update descriptions explain new-versus-existing behavior;
   conditional omission cannot be expressed as an unconditional JSON required
   field. Node variants must not globally require `on_error` for gates or
   structural kinds; retain queue-specific refusal rules for authored routing
   fields. The splice nested transform advertises required `on_error`. Update
   schema/model parity, required-path consumers and generated catalog pins from
   the actual producer where affected, never by hand-invented values.
8. `src/elspeth/web/composer/prompts.py` and `docs/reference/composer-tools.md`:
   shared planner guidance states explicit choices and coverage limits. No
   tutorial-only prompt, backend branch, fixed-script change, server-authored
   route, or provider bypass is introduced.

The helper stays in the existing Composer tool layer unless actual dependency
gates demand a lower shared module. If YAML parsing would create an invalid
import direction, use an owned lower Composer parsing module and update every
caller directly; no re-export or compatibility alias.

## YAML, runtime and round trip

`src/elspeth/core/config.py` keeps its existing language: required transform,
aggregation and sink failure fields; optional gate failure field. Source failure
routing remains inside plugin `options`, as defined by
`src/elspeth/plugins/infrastructure/config_base.py` and source config classes.
No new runtime setting, null interpretation, or required field is added.

`src/elspeth/web/composer/yaml_importer.py` already requires transform and sink
failure strings. Source import must stop accepting missing/null/empty routes as
discard on every currently public Composer import. Its actual production
callers are the authenticated session YAML route
(`src/elspeth/web/sessions/routes/composer/state.py`) and workflow-library fork
(`src/elspeth/web/sessions/routes/workflow/library.py`); neither is a runtime
resume caller. No plugin-name, source-name, authentication, workflow-ownership
or catalog-visibility exception is admitted. Do not add a trusted-import switch
or a new internal caller. Public null-source omission is refused; an explicit
choice proceeds only to the existing plugin and policy admission checks.
Direct runtime `NullSource({})` remains valid without configuration. Composer
authoring/import is narrower than runtime-owned resume scaffolding; this design
does not promise that Composer imports every runtime-valid internal placeholder.

Canonical ordinary runtime YAML supplies the choice
in `sources.<name>.options.on_validation_failure`. The importer's currently
admitted entry-level spelling remains accepted when it carries an explicit
nonempty choice; conflicting entry/options values remain rejected. A supplied
null, empty or wrong-type spelling is invalid even if another spelling contains
a value; it must not be silently replaced or coerced from a boolean/number.
Validate each supplied position independently, then trim each valid string,
then compare canonical values. Entry `" quarantine "` and options
`"quarantine"` therefore agree; different canonical names conflict without
echoing either value. Matching explicit spellings lower to one canonical
SourceSpec field. The ordinary valid YAML language is preserved; formerly
accepted ordinary source omissions were Composer fabrication rather than valid
ordinary source plugin configuration. The public-import constraint above
deliberately excludes the runtime-only omission behavior of internal NullSource.

`yaml_generator.py` continues emitting the SourceSpec choice in source options,
node choices at node level, and sink write choices at sink level. Canonical
state serialization (`state.py` and `tools/_common.py`) preserves admitted
routes. Import -> canonical state -> YAML -> runtime config -> reimport must
preserve explicit discard and named-route behavior; gate omission stays omitted
and explicit gate null remains fail-fast. Existing blob/path custody,
normalization and source-review requirements remain in place.

## Discriminating synthetic controls

Before production edits, add failures proving omitted new transform,
aggregation, source (all three tools), sink, full-state components and splice
are rejected without state/version/proposal mutation. Check owned diagnostic
field paths and public value privacy. Pair each with explicit discard and
explicit named-route controls; acceptance of a route value is distinct from
whole-graph validity or coverage eligibility.

Partial-update controls must prove a named policy and explicit discard survive
omitted routing, null/empty reject without resetting, changed kind does not
inherit, and options-only patches preserve policy. Gate omission/null/fail-fast
and explicit gate routing remain legal under the existing runtime contract.
Structural kinds retain their existing contracts. Full replacement must not
borrow choices from prior state. Already-applied splice remains idempotent.
Include same-identity source plugin replacement retaining its named route,
different source identity refusing inheritance, and stale/empty prior choices
refusing. Exercise omission versus explicit null through redaction,
argument-model reconstruction and provider schema/wire round trips for every
update surface; schema-injected null must not masquerade as omission.

Required-control controls must show malformed ordinary candidates cannot gain
coverage by fallback, policy-owned explicit routes survive, ordinary covered
error-to-quarantine still fails existing coverage, explicit discard remains
authorable, and author-around-error refuses. No tutorial stimulus changes.
Test the complete mutation result after required-control projection refuses,
including absence of state/version/proposal changes; checking only the private
projection return value is insufficient. Retain existing failsink eligibility
and before-write coverage negative controls after explicit choice resolution.

YAML controls cover required source options, existing entry spelling, conflicting
spellings, supplied null/empty in either position, explicit discard/named route,
valid runtime config round trip, gate omission, multiple sources, and unchanged
source blob custody. Add valid-and-equal-after-trim source spellings, invalid
null/empty/wrong-type in either supplied position despite a valid sibling, and
conflicting canonical names with no public scalar echo. Public session and
workflow-library imports must refuse null-source omission without a plugin-name
exception. New planner source/full-state omission cannot evade the contract
with null either; direct runtime `NullSource({})` remains valid. An explicit
failure choice for a null-source entry proceeds only to ordinary plugin/policy
admission. Use actual
runtime config/plugin validation for parity,
not only parser snapshots. Run affected schema/model declaration parity,
wire-shape and whole-tree AST gates plus focused serial tool, importer,
generator, state and required-control tests. Shared authoring effects require
root-coordinated broad qualification before merge. No live provider calls.

## Review and implementation boundary

Independent Astra LLM/correctness and systems/security review must settle this
contract before source edits. Material unresolved changes go to the maintainer;
routine implementation details follow the reviewed contract. Source review,
focused controls, broad qualification, integration, publication and release are
separate states. #171 is not fixed by this design document.
