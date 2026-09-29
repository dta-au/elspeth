# Composer LLM contract and source approval repair implementation plan

> **Status, 2026-09-30:** Retained acceptance and repair reference for
> `release/0.8.1`. The task list below records the 2026-09-21 plan; unchecked
> boxes are not a current missing-work inventory. Reconcile each requirement
> against the candidate before implementing a change.

The repair branch `fix/composer-llm-contracts-20260921` at `66d25ac7a` contains
an implementation of these workstreams. Source inspection at the recovery
base `a2f0281ff` found the generated-input helper and configuration validator
in `plugins/transforms/llm/base.py`, its Stage-1 caller in `composer/state.py`,
and `reconcile_authoritative_reviews` in `composer/tools/sources.py`. These
are implementation evidence, not proof that every branch assertion or live
acceptance milestone passed. The worktree recovery must preserve distinct
missing regression coverage and rerun the affected checks; do not apply the
old branch wholesale over newer production code.

Guided Composer has been removed under the
[complete removal plan](2026-09-28-guided-mode-removal.md). Teaching changes
and provider-backed acceptance now target the ordinary freeform Composer
and shared tutorial path. The retired Guided teaching path listed in the
original branch is not an implementation target. Legacy `elspeth-*` IDs below
are historical references; use GitHub Issues for current coordination and do
not publish or close records as part of document recovery.

**Goal:** Make the reported colour workflow authorable, valid, and executable with one multi-query LLM node, while retaining approval of unchanged source content during subsequent repairs.

**Architecture:** Keep the provider responsible for authoring the graph. Correct the planner's input/output vocabulary, reject contradictory declarations at both configuration and Composer admission, and make the plugin's output model describe successful rows accurately. Restore source approvals through the existing authoritative review reconciler, then exercise the complete workflow through real validation, persistence, and execution boundaries.

**Tech stack:** Python, Pydantic, pytest, Composer tools and session services, ELSPETH graph builder/executor, SQLite and PostgreSQL test infrastructure, and the existing Composer evaluation harness.

**Historical baseline:** Reviewed `release/0.8.1` at `bfda1fb1630d26b912452becb9a41a9840da5cee` on 2026-09-21. Recheck HEAD and the full dirty-path inventory before implementation. Concurrent unrelated document, authentication, and LLM provider changes were present during planning, including overlapping `llm/base.py` and `llm/transform.py` edits; preserve them and coordinate the implementation base with their owner before integrating overlapping hunks. Recovery targets local `release/0.8.1`; publication and deployment are separate actions.

## Scope and evidence

The five workstreams below correspond to the five repair items in the investigation handoff. Workstream 5 is the regression and acceptance work that joins the repairs; it is not a claim of a fifth independently reproduced backend defect.

| Item | Required result | Primary owner |
| --- | --- | --- |
| 1. Authoring guidance | LLM input declarations contain upstream fields; generated answers are declared by query output definitions | Composer teaching surfaces and plugin assistance |
| 2. Contradictory input guard | Generated fields cannot also be explicitly required as inputs to that same multi-query LLM | LLM configuration and Composer Stage 1 |
| 3. Producer schema | Successful structured answers have their actual non-null types and required presence | LLM output-schema builders |
| 4. Source approval retention | A route-only rebind of unchanged approved source content retains the complete proof | Source tool mutation and review reconciliation |
| 5. Workflow regression | The colour pipeline survives authoring, approval, repair, validation, persistence, and execution | Cross-boundary integration tests and provider evaluation |

Measured before planning:

```text
good_colour_pair_answer: annotation=str | None; required=False;
                        declared_output=True; guaranteed=True
approximate_hex_answer: annotation=str | None; required=False;
                        declared_output=True; guaranteed=True
```

The source handler was also exercised with a real temporary session database and blob using `TestEchoedServerOwnedMetadata._bound_source` and `_resolved_state`. These fixtures construct the same coherent artifact-bound proof the resolver stores:

```text
handler_success: True
before_status: resolved
after_status: pending
after_existing_reconciler: resolved
review_metadata_restored: True
route_preserved: retained_failures
```

This refines the earlier helper-only investigation: `invented_source` approval is bound to `accepted_artifact_hash`, not `resolved_prompt_template_hash`. Supplying `current_field_value=content` to the generic pending-requirement helper is not the repair. Use `reconcile_authoritative_reviews`.

Today’s `6211043b` patch changed prompt-role validation and guidance; the defective producer and source-rebind lines predate it. Changed guidance may have exposed these paths, but that causal attribution remains unproven without the original session trace or a controlled provider comparison. Do not label a rollback of that patch as the fix.

The report also describes mutation during a bug-report request and a withheld final answer. Their exact session is absent from the current local session database. Include explicit acceptance checks for both in Task 5; do not claim those symptoms repaired solely because schema tests pass.

## Preparation and execution order

- [ ] Create an isolated implementation worktree using the repository's worktree conventions. Do not switch the shared checkout. Read `CONTRIBUTING.md` and `src/elspeth/plugins/transforms/AGENTS.md` before editing code.
- [ ] Set the implementation checkout as the working directory and verify imports with both source roots on `PYTHONPATH`. The relative commands below run from that checkout, not from a remembered shell directory.

```bash
cd "$(git rev-parse --show-toplevel)" && \
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -c \
  'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

- [ ] Use the archived records for `elspeth-a10d15055b` and `elspeth-5a372d3267` only to recover their distinct acceptance criteria and any canonical GitHub mapping. The former concerns missing declared query inputs; the new guard concerns generated outputs declared as inputs. Do not conflate them or recreate retired local tracking.
- [ ] Establish the exact colour fixture and the failing producer edge first. Implement Task 3, then Tasks 1 and 2, then Task 4, and finish Task 5. Task 4 can be developed independently in a separate worktree if delegation is requested.
- [ ] Use lane-private logs under `.claude/lanes/composer-llm-contract-repair/`. Define this shell helper once in the implementation shell; each call uses a distinct label (for example `task-1-red`, then `task-1-green`). It captures the actual pytest exit code and preserves worktree import provenance. Read the printed log only after the call completes.

```bash
REPAIR_ROOT="$(git rev-parse --show-toplevel)"
mkdir -p "$REPAIR_ROOT/.claude/lanes/composer-llm-contract-repair"
run_repair_tests() {
    local label="$1"
    shift
    local log="$REPAIR_ROOT/.claude/lanes/composer-llm-contract-repair/$label.log"
    cd "$REPAIR_ROOT" || return
    local test_status=0
    PYTHONPATH="$REPAIR_ROOT/src:$REPAIR_ROOT/elspeth-lints/src" \
      .venv/bin/python -m pytest \
      -o "pythonpath=$REPAIR_ROOT/src $REPAIR_ROOT/elspeth-lints/src" \
      "$@" > "$log" 2>&1 || test_status=$?
    printf 'exit=%s log=%s\n' "$test_status" "$log"
    return "$test_status"
}
```

Each task is a reviewable commit after its focused checks. Run `scripts/branch-safety-check.sh --intent commit` and stage only owned paths before any commit.

## Task 1: Correct all LLM input/output authoring guidance

**Modify:**

- `src/elspeth/web/composer/planner_authoring_aids.py`: `_LLM_OUTPUT_CONTRACT_RULES`.
- `src/elspeth/plugins/transforms/llm/transform.py`: `LLMTransform.get_agent_assistance` hints and relevant examples.
- `src/elspeth/web/composer/skills/pipeline_composer.md`: LLM schema and multi-query instructions.
- Historical branch also changed `composer/guided/skills/step_3_transforms.md`; that surface is retired. Verify equivalent teaching on the maintained freeform surfaces above.

**Tests:** `tests/unit/web/composer/test_planner_authoring_aids.py`, `tests/unit/plugins/test_catalog_reference_content.py`, and the integration scenario in Task 5.

- [ ] Add focused assertions against the rendered planner aids and live LLM assistance. Require explicit input/output separation and both supported query forms. Prove the assertions detect the current contradictory text. Avoid pinning entire prompt strings when an assertion over a contract statement suffices.
- [ ] Run those tests and capture the expected failure: the current aid tells the planner that the node-level schema declares generated prefixed fields.
- [ ] Replace the contradictory sentences with this guidance, adapted only for each surface's formatting:

```text
An llm transform's options.schema describes the INPUT row supplied by its
upstream producer. options.required_input_fields lists upstream row columns
the node requires. Do not put fields generated by this node into either
input declaration, or hand-add their producer guarantees to options.schema.

For multi-query mode, queries.<name>.input_fields maps template variable
names to upstream column names. queries.<name>.output_fields declares the
generated structured fields. The plugin supplies their output contract:
each suffix becomes <query_name>_<suffix>. Downstream mappers and sinks
refer to those exact names.

For a colour input and two answer queries, the LLM's schema and
required_input_fields name only colour; good_colour_pair_answer and
approximate_hex_answer belong to the downstream mapper and sink schemas.

Single-query output_fields also creates typed row fields, named by their
unprefixed suffixes. A free-text request for JSON keys alone does not.

Keep required fields and types on downstream consumers that actually use
them. When validation identifies a plugin-generated contract contradiction,
report that contradiction; do not erase the consumer's requirements to
silence the error.
```

- [ ] Preserve the recent system/user prompt rules. Retain one shared `system_prompt`, query-specific `template` values, and the supported node-level fallback. Correct any sentence claiming a fallback is mandatory when all queries supply templates; the configuration model permits this form.
- [ ] Re-run the focused tests. Generate authoring aids through the catalog and verify their content, rather than relying only on source-text searches. Task 5 supplies behavioral proof that the teaching can produce a valid graph.

```bash
run_repair_tests task-1-green \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/unit/plugins/test_catalog_reference_content.py -n 0
```

**Done when:** every maintained teaching surface distinguishes input columns from generated outputs; the two-query example validates without placing outputs into the LLM input schema.

## Task 2: Reject generated outputs explicitly required as multi-query inputs

**Modify:**

- `src/elspeth/plugins/transforms/llm/base.py`: shared semantic check and an `LLMConfig` after-validator.
- `src/elspeth/web/composer/state.py`: Stage-1 counterpart beside `_validate_multi_query_required_input_columns`, invoked from the LLM validation dispatch.
- `src/elspeth/web/composer/tools/generation.py`: closed error code and repair explanation.

**Tests:** `tests/unit/plugins/llm/test_llm_config_multi_query_contract.py`, `tests/unit/web/composer/test_state_multi_query_contract.py`, `tests/unit/plugins/test_validation_path_agreement.py`, and affected closed-code fixtures under `tests/unit/web/composer/fixtures/`.

The rule is narrow: reject intersection with this node's generated names, not every declared field absent from a prompt. Extra upstream presence assertions, image fields, query aliases, `row.source_row` references, and the explicit `required_input_fields: []` opt-out retain their existing semantics. This is not an equality check between prompt reads and required inputs.

- [ ] Add configuration and Stage-1 failing cases for each answer field in `required_input_fields`; repeat with mapping-form and list-form queries and a custom `response_field`.
- [ ] Include raw response names and operational fields (`_usage`, `_model`) in the generated-name set. Include explicit `schema.required_fields` and image-input declarations in the required-input set. A literal query input mapping that consumes its own generated name is also contradictory and must be rejected even when `required_input_fields` is empty.
- [ ] Keep ordinary `schema.fields` demotion behavior unchanged: `BaseTransform.input_schema` already distinguishes created and consumed fields. Do not rewrite that shared mechanism or globally forbid input/output overlap for other transforms.
- [ ] Use one pure function over resolved `QuerySpec` values. The following proposed helper belongs in `base.py`; both admission surfaces use it after parsing:

```python
from collections.abc import Iterable, Sequence

from elspeth.plugins.transforms.llm import LLM_GUARANTEED_SUFFIXES
from elspeth.plugins.transforms.llm.multi_query import QuerySpec


def multi_query_generated_input_conflicts(
    specs: Sequence[QuerySpec],
    response_field: str,
    required_fields: Iterable[str],
) -> tuple[str, ...]:
    generated: set[str] = set()
    consumed = set(required_fields)
    for spec in specs:
        generated.update(
            f"{spec.name}_{response_field}{suffix}"
            for suffix in LLM_GUARANTEED_SUFFIXES
        )
        generated.update(
            f"{spec.name}_{field.suffix}"
            for field in (spec.output_fields or ())
        )
        consumed.update(spec.input_fields.values())
    return tuple(sorted(generated & consumed))


def multi_query_generated_input_message(fields: Sequence[str]) -> str:
    names = ", ".join(repr(name) for name in fields)
    return (
        f"This multi-query llm node generates {names}, but also requires "
        "those names as inputs. Input declarations and query input_fields "
        "must refer to upstream columns. Keep generated outputs in "
        "queries.*.output_fields and downstream schemas; remove them from "
        "this node's input requirements or choose distinct output names."
    )
```

- [ ] Add the `LLMConfig` after-validator. Resolve queries with `resolve_queries(self.queries)`, union `self.declared_input_fields` with `self.schema_config.required_fields or ()`, and call the helper. For direct static `row.source_row` reads, also pass the union returned by `multi_query_source_row_columns(self.effective_template(spec.template))`. On a conflict, raise `ValueError(multi_query_generated_input_message(conflicts))` inside Pydantic validation. Do not convert framework errors into configuration errors.
- [ ] Parse raw Composer query definitions into `QueryDefinition` with Pydantic at the boundary, preserving mapping/list forms and using `deep_thaw` for frozen values. Resolve them with the same function; use the same required-input and direct-column extraction. Return one high-severity `ValidationEntry` on `node:<id>`, with new code `query_generated_fields_required` and the shared message. Malformed query/config shapes remain owned by existing plugin-options validation; tests must prove malformed values do not create a 500 or silently become valid.
- [ ] Add the code to the closed explanation vocabulary in `generation.py`. The repair must identify this LLM node, instruct the planner to preserve generated output definitions, and name upstream-only inputs. It must not propose fabricating columns on the source, weakening downstream consumers, or setting `required_input_fields: []` as a repair.
- [ ] Add the plugin rejection to the live validation-path agreement cases; regenerate any changed closed-code fixture with its producer and review the exact diff. Do not hand-invent wire fixtures.
- [ ] Run configuration, Composer, and invariants tests. Positive controls: aliases with `input_fields={"value": "colour"}`; two queries reading the same colour; list-form queries; custom response names; valid image input; static `row.source_row`; empty opt-out with no output collision; single-query configurations unchanged. Negative controls: each explicit or actual-consumption path to a generated name.

```bash
run_repair_tests task-2-green \
  tests/unit/plugins/llm/test_llm_config_multi_query_contract.py \
  tests/unit/web/composer/test_state_multi_query_contract.py \
  tests/unit/plugins/test_validation_path_agreement.py \
  tests/invariants/test_transform_input_contract_is_satisfiable.py \
  tests/invariants/test_input_schema_config_is_captured.py -n 0
```

**Done when:** YAML/plugin admission and Composer report the same contradiction before graph execution, including the repair message, without removing valid input-contract behavior.

## Task 3: Make successful LLM output schemas truthful

**Modify:** `src/elspeth/plugins/transforms/llm/__init__.py`: `_build_augmented_output_schema`, `_build_multi_query_output_schema`, and `_build_llm_output_schema_config`; `src/elspeth/plugins/transforms/llm/transform.py`: the two constructor branches that create these contracts.

**Tests:** `tests/unit/plugins/llm/test_transform.py`, `tests/unit/plugins/llm/test_audit_metadata_functions.py`, `tests/integration/plugins/llm/test_contract_validation.py`, and `tests/unit/web/execution/test_validation_edge_contract_disclosure.py`.

- [ ] Add a failing test using a flexible LLM input schema containing only `colour: str`, two queries each declaring `answer: string`, and a downstream select-only mapper with all three business fields required as `str`. Build real plugin models and a real execution graph; reproduce both `str | None` errors.
- [ ] Extend the existing output-model tests to assert types and presence, not just field names. For the two answer fields:

```python
for name in ("good_colour_pair_answer", "approximate_hex_answer"):
    field = transform.output_schema.model_fields[name]
    assert field.annotation is str
    assert field.is_required()
    assert name in transform.declared_output_fields
    assert name in transform._output_schema_config.get_effective_guaranteed_fields()
```

- [ ] Add the single-query structured equivalent. Parameterize generated values across string, integer, number, boolean, and enum. Prove both missing and explicit `None` values are rejected; valid values are accepted. Use complete successful-row fixtures with response, usage, and model fields so tests do not fail for an unrelated omitted field.
- [ ] Change the generated fields in both schema builders from optional to required. Preserve existing pass-through input definitions and the observed-schema branch. Generated response and model fields use their existing `str` type; usage remains the existing `any` type with required presence. Do not infer stronger nested usage semantics from the new requirement.

```python
FieldDefinition(name=prefix, field_type="str", required=True)
FieldDefinition(name=name, field_type=_SUFFIX_SCHEMA_TYPES[suffix], required=True)
FieldDefinition(name=field_name, field_type=field_type, required=True)
```

- [ ] Update builder docstrings to describe required, correctly typed successful outputs. Preserve output openness for upstream pass-through fields, audit metadata placement, and atomic failure routing. Do not change the global schema factory, coalesce nullability rules, or every plugin's schema behavior.
- [ ] Cover already-declared generated names explicitly. A legacy authored field must not silently mask the plugin's actual output type or reintroduce optional successful outputs. Reuse the existing input-demotion semantics, and have the output builder replace definitions of its own generated names with their canonical required types. Preserve the original position for replaced definitions; append genuinely new fields in current order. Prove this for an optional legacy declaration and for a conflicting legacy type. Explicit required input/output contradictions are rejected by Task 2.
- [ ] Canonicalize plugin-owned fields in BOTH the Pydantic model and `_output_schema_config`. This is required, not optional cleanup: `_apply_declared_output_field_contracts` derives emitted row contracts from `_output_schema_config`, and Composer's type resolver also reads it. Updating only the Pydantic builder would leave a legacy `answer: int` definition on a string result, causing a runtime contract failure; a legacy `answer: str?` would retain optional/nullable metadata.
- [ ] Build one tuple of canonical generated `FieldDefinition` values from each constructor branch's resolved output names and types. Add it as a required keyword argument to `_build_llm_output_schema_config` and update both call sites. Reuse the same field-construction/merge helper in both Pydantic builders. For explicit schemas, replace matching base definitions and append new generated definitions; leave pass-through field objects untouched. The scoped merge can be implemented as:

```python
def _merge_llm_generated_fields(
    base_fields: tuple[FieldDefinition, ...],
    generated_fields: tuple[FieldDefinition, ...],
) -> tuple[FieldDefinition, ...]:
    generated_by_name = {field.name: field for field in generated_fields}
    existing_names = {field.name for field in base_fields}
    return (
        *(generated_by_name.get(field.name, field) for field in base_fields),
        *(field for field in generated_fields if field.name not in existing_names),
    )
```

`FieldDefinition` is already imported in the target module. Construct generated definitions with `required=True, nullable=False` and the actual schema type. Retain guarantee union, audit policy, and explicit-mode output behavior. Preserve the observed-mode dynamic Pydantic model and its existing guarantee participation; test its runtime contracts separately instead of attaching a fabricated closed schema.
- [ ] Extend the legacy tests through successful `_process_row`/executor emission, not just model inspection: assert the final `PipelineRow` contract declares each generated string as `str`, required, and non-nullable for both single-query and multi-query cases. An observed-input case must still execute and preserve upstream data. Assert invalid declared consumer types are caught in both Composer and runtime graph validation after typed generated config fields replace prior `any` entries.
- [ ] Run the real trained-operator validation entry point with the exact colour graph. It must pass without widening the field mapper or sink to nullable/`any`, and without changing their fields to observed mode. Retain an unrelated genuine producer/consumer mismatch as a negative control for the validator.
- [ ] Run existing extraction-failure tests to prove malformed JSON, missing output keys, wrong value types, and one-query failure still fail the row atomically. Do not route partial answers onto the success edge.

```bash
run_repair_tests task-3-green \
  tests/unit/plugins/llm/test_transform.py \
  tests/unit/plugins/llm/test_audit_metadata_functions.py \
  tests/integration/plugins/llm/test_contract_validation.py \
  tests/integration/plugins/llm/test_multi_query.py \
  tests/unit/web/execution/test_validation_edge_contract_disclosure.py -n 0
```

**Done when:** the real graph accepts truthful non-null consumers of successful LLM answers, and the plugin continues to reject malformed results instead of emitting partial success.

## Task 4: Preserve source approval through unchanged source rebinding

**Modify:** `src/elspeth/web/composer/tools/sources.py`: `_execute_set_source_from_blob` immediately after constructing the proposed source state.

**Reuse unchanged:** `src/elspeth/web/interpretation_state.py`: `reconcile_authoritative_reviews` and `_reconcile_source_options`.

**Tests:** `tests/unit/web/composer/test_promote_set_source_from_blob.py`, `tests/unit/web/composer/test_authoring_reconciliation.py`, `tests/unit/web/composer/test_source_interpretation_requirement_guard.py`, and the persistence test in Task 5.

- [ ] Add a regression to `TestEchoedServerOwnedMetadata` using its existing `_bound_source` and `_resolved_state` helpers. Rebind the same `blob_ref`, send only the operational schema, change `on_validation_failure` to a retained-failure output, and omit all approval metadata from the caller. Assert the handler's source retains the exact requirement record and full `source_authoring` metadata; assert the route changes.
- [ ] Run it before the patch; expected failure is `resolved -> pending` plus loss of the source's approval metadata. Also test a second identical call for idempotence.
- [ ] Apply the existing source-patch reconciliation pattern in `_execute_set_source_from_blob`:

```python
proposed_state = state.with_named_source(source_name, source)
try:
    new_state = reconcile_authoritative_reviews(state, proposed_state)
except (KeyError, TypeError, ValueError) as exc:
    return _failure_result(
        state,
        review_reconciliation_failure_message(
            exc, retry_hint="Re-inspect the pipeline and retry."
        ),
        error_code="review_reconciliation_failed",
    )
```

The referenced functions are already imported by `sources.py`. Keep the existing mutation result, blob payload, affected-component identity, version increment, path policy, custody lock, ready/hash validation, and echo/forgery admission gates.

- [ ] Test the entire public `execute_tool("set_source_from_blob", ...)` path as well as the handler. Set `validate_arguments=True` and `require_data_dir_for_paths=True`, supply the real temporary data directory and session operation context/authority, and leave `_interpretation_requirements_are_internal=False`. Prefer the real compose dispatcher in the integration test. Resolve approval through the real review service, persist/reload the resulting state, and compare event counts as well as fields. Do not accept a helper-only status assertion as completion.
- [ ] Test the preservation/invalidation boundary:

| Proposed operation | Required outcome |
| --- | --- |
| Same named source, plugin, blob, content, new failure route | Preserve full proof; update route |
| Same operation repeated or replayed after reload | Remain resolved; no duplicate review event |
| Caller omits review metadata or echoes an exact stored projection | Recover authoritative evidence from stored state |
| Same blob ID with changed content hash | Pending; fresh review required |
| Different content, source name, or plugin | Do not transfer old approval |
| Different blob ID with identical bytes | Follow the existing reconciler's content-bound semantics; do not add a new blob-ID authorization rule |
| Caller forges resolved status, event ID, accepted hash, or authoring metadata | Reject through existing admission gates |
| Stored resolved proof has a stale artifact hash or incoherent evidence | Fail closed with reconciliation diagnostics; do not silently mark resolved |
| Another named source or LLM node has its own reviews | Preserve independent review semantics; no proof crosses components |

- [ ] Verify route-only `set_pipeline` and `patch_source_options` equivalents still use their existing reconciliation and remain consistent. The plural blob source has different review semantics; test that this change does not alter them instead of unconditionally adding review propagation there.

```bash
run_repair_tests task-4-green \
  tests/unit/web/composer/test_promote_set_source_from_blob.py \
  tests/unit/web/composer/test_authoring_reconciliation.py \
  tests/unit/web/composer/test_source_interpretation_requirement_guard.py \
  tests/unit/web/composer/test_blob_inline_tools.py \
  tests/unit/web/composer/test_set_source_from_blobs.py -n 0
```

**Done when:** the full tool and persisted session retain coherent source proof for unchanged content, while changed content and forged proof still require review or fail admission.

## Task 5: Prove the incident workflow across all boundaries

**Create:** `tests/integration/web/composer/test_colour_multi_query_contract_repair.py` and `evals/composer-harness/scenarios/hardmode/colour_multi_query_contract_repair.json`.

**Reuse:**

- `tests/integration/web/composer/test_prompt_review_card_end_to_end.py`: real review staging, resolution, and execution materialization pattern.
- `tests/integration/web/composer/test_freeform_proposal_prevalidation.py`: real Composer loop with scripted provider responses.
- `tests/unit/web/execution/test_validation_edge_contract_disclosure.py`: real trained-operator graph-validation path.
- `tests/integration/plugins/llm/test_multi_query.py`: provider double, executor, and audit setup.
- `evals/composer-harness/README.md` and `evals/composer-harness/hardmode/RUNBOOK.md`: maintained live evaluation workflow.

Use a module-local fixture unless a second real consumer needs it. Do not add a general-purpose test framework. Script only the external provider boundary; keep tool dispatch, reviews, graph construction, serialization, and execution real.

- [ ] Define five source rows: `red`, `blue`, `green`, `yellow`, `purple`. Use the following exact LLM business configuration (test provider binding is supplied by the existing fixture):

```yaml
system_prompt: Give concise factual colour answers in the requested format.
prompt_template: Answer the question about {{ row.colour }}.
schema:
  mode: flexible
  fields: ["colour: str"]
required_input_fields: [colour]
queries:
  good_colour_pair:
    input_fields: {colour: colour}
    template: What colour pairs well with {{ row.colour }}?
    response_format: structured
    output_fields: [{suffix: answer, type: string}]
  approximate_hex:
    input_fields: {colour: colour}
    template: What is an approximate hex value for {{ row.colour }}?
    response_format: structured
    output_fields: [{suffix: answer, type: string}]
```

- [ ] Keep one multi-query node with separate prompt calls. Configure the field mapper with `select_only: true`, identity mapping for `colour`, `good_colour_pair_answer`, and `approximate_hex_answer`, and required string input fields. Configure the CSV sink with a fixed schema in that order. Configure separate retention outputs for source validation and LLM errors so the repair can be exercised without dropping failed rows.
- [ ] Stage and approve the invented source with the real review resolver. Resolve every other review required by the actual policy and authored prompts, including any authored semantics of “pairs well.” Record the source event ID, accepted artifact hash, authoring metadata, and current review count.
- [ ] Drive the provider-authored valid graph through Composer admission and production execution validation. Assert the provider was called at the relevant authoring transition. Assert exactly one LLM transform and two queries; no server-authored graph shortcut is allowed.
- [ ] Submit the malformed input contract from the report. Assert Task 2 reports `query_generated_fields_required` at the LLM node. Supply a scripted provider repair containing only upstream input fields. Assert subsequent graph validation succeeds without relaxing mapper or sink fields.
- [ ] Apply a retention-route change through `set_source_from_blob`, then persist and reload. Assert source blob/hash and the complete approval proof are unchanged, the route is updated, no new invented-source review appears, and execution materialization is not blocked by duplicate approval.
- [ ] Execute with deterministic provider responses. Measure exactly ten query calls for five successful rows, distinct query prompts carrying the current colour, and a five-row CSV whose header is exactly the three requested business columns. Operational usage/model fields remain available internally but do not leak into this output file.
- [ ] Force one query on one row to return malformed or missing structured output. Assert that row produces no partial success CSV row and follows the configured error-retention route with the original colour and failure evidence. The other rows retain correct answers.
- [ ] Exercise source-failure retention separately: start with five valid rows plus one deliberately ragged CSV row, and approve those exact bytes before changing the source failure route. Assert the invalid row reaches the new source-retention output with rejection evidence, makes no LLM calls, and never reaches the success CSV; the five valid rows still produce ten query calls and five successful outputs. Do not change approved bytes to inject this fault and then expect approval to survive.
- [ ] Exercise the request, “Write a bug report explaining the validation failure; do not change the pipeline,” against BOTH a persisted invalid graph and the repaired graph. In the invalid case keep the source proof resolved while the answer edge is invalid; independently exercise missing review/proof diagnostics and an advisor `FLAGGED` result. Construct a fresh service/repair ledger for the first reporting request, and repeat after reload: a previously consumed repair budget must not hide the defect. Script a provider that attempts a graph mutation after repair feedback as a negative control. Assert graph identity and source approval evidence stay unchanged, no graph-mutating tool succeeds, provider/advisor calls remain bounded, and a visible provider-written report or explicit advisor-block explanation survives response delivery and message reload. Record successful report delivery separately from explicit advisor blocking. A hidden markdown artifact alone is not success.
- [ ] Inspect the no-tool continuation seams before declaring this case fixed: `ComposerServiceImpl._try_terminate_no_tools` in `src/elspeth/web/composer/service.py` can inject proof/preflight repair and calls `_evaluate_terminal_no_tool_advisor_gate` with `allow_repair_continue=True`. The source existence or graph invalidity must not itself turn a diagnostic request into authority to mutate. If the new test fails, trace the admitted request and tool-batch authority into these branches and implement the bounded correction there, covering both repair-feedback and attempted-tool paths. Do not turn `classify_pipeline_mutation_intent` into a new permission gate: its documented role is routing, and actual authority lives in the admitted workflow/tool-batch guards. Run `tests/unit/web/composer/test_advisor_terminal_publication.py` and the existing no-tool disclosure tests with the integration case. The incident remains open if this acceptance case cannot be satisfied.
- [ ] Repeat deterministic admission and approval checks through the current freeform proposal and tutorial callers of these same tools. Guided proposal binding is retired. Do not add a tutorial-specific path. Existing prompt-review end-to-end coverage must remain green.
- [ ] Use a prompt-keyed deterministic provider double for execution: select responses by the colour and query identity in the received prompt, not call order, because parallel query scheduling is permitted. Do not copy the freeform test harness's unconditional `CLEAN` advisor stub into advisor rejection tests; provide explicit `CLEAN` and `FLAGGED` outcomes.
- [ ] Add the live scenario to the maintained harness using persona `p1_compliance`. Omit upload fields (`csv_filename`/`csv_content`) so the planner creates an invented CSV source and the required review actually occurs. Use this opening prompt: “Invent a small CSV with five different basic colour names in a colour column. For each colour, use one multi-query LLM node with two separate prompts: one asks for a good colour pairing and the other for an approximate hex value. Save exactly colour, good_colour_pair_answer, and approximate_hex_answer to colour_answers.csv. Preserve source-validation failures and LLM failures in separate outputs.” The live names may differ from the deterministic five-colour fixture; assert their correspondence across source, prompts, and output rather than forcing a canned provider answer. The scenario's stop conditions require all milestones below; “pipeline ready” alone is not DONE:

| Milestone | Live evidence and pass condition |
| --- | --- |
| Provider authoring | Tool audit and final YAML show one two-query LLM, upstream-only input contract, and exact typed consumer fields |
| Source approval | A resolved invented-source event binds the generated CSV content hash |
| Route change | Failure route changes; source proof and resolved event identity remain; no duplicate pending review |
| Reporting | Bug-report follow-up produces visible content; graph and source proof remain unchanged |
| Execution | Explicitly run the approved graph and inspect the three-column, five-row result; actual calls carry each colour |

Drive these via the existing harness/persona protocol. After resolving the actual required review events, use the route-change follow-up: “Keep the source data and processing unchanged. Send source-validation failures to a new retained output named source_failures_revised.” Then use the exact report-only request above before running the approved graph. Keep the established five user-turn budget; review-resolution API calls are not extra chat turns. If setup, approval, or provider repair exhausts the budget before a milestone, record that milestone as unverified and the scenario as incomplete, not passed. No harness-wide early-exit policy change is needed: the scenario's own stop conditions govern completion.

- [ ] In addition to the harness's standard outputs, capture the real interpretation history with `GET /api/sessions/{session_id}/interpretations`, and paginate `GET /api/sessions/{session_id}/messages` with `include_tool_rows=true`, `include_llm_audit=true`, `include_raw_content=true`, and `include_rejection_reasons=true` (limit at most 500, advancing offset). Use the canonical redacted invocation audit envelopes for tool names/status and before/after graph snapshots for mutations; never depend on raw credential-bearing arguments. Store observations and a pass/fail/unverified verdict for every milestone in the run directory. The standard finalizer's LLM-only sidecars are insufficient proof of source review identity or successful tool mutations.
- [ ] Run the live scenario against the repaired build with the normal configured planner and advisor, through `evals/composer-harness/hardmode/harness.sh colour_multi_query_contract_repair` and the existing turn/finalization scripts documented in its runbook. Keep credentials and session artifacts out of commits. Live provider evaluation is a separate evidence result from mocked-provider tests; if unavailable, report it as unverified and do not claim the planner behavior is proven.

```bash
run_repair_tests task-5-green \
  tests/integration/web/composer/test_colour_multi_query_contract_repair.py \
  tests/integration/web/composer/test_prompt_review_card_end_to_end.py \
  tests/integration/web/composer/test_freeform_proposal_prevalidation.py -n 0
```

**Done when:** every deterministic acceptance check passes and the live planner/advisor outcome is recorded separately. The runtime test verifies exact output values, columns, row count, calls, and failure routing; the authoring test verifies that the provider performs the authoring work.

## Integration gates and delivery

- [ ] Read every touched file and review the final diff for widened permissions, auto-authored pipeline structure, duplicated source-proof logic, unnecessary schema weakening, and unrelated changes.
- [ ] Run Ruff and mypy. Update plugin `source_file_hash` after formatting using `scripts.cicd.plugin_hash.compute_source_file_hash`; refresh only affected manifest/provenance pins through their documented producers. Run `tests/integration/core/dag` and `tests/unit/architecture` when plugin source identities change.
- [ ] Run affected whole-tree gates from `CONTRIBUTING.md`: dynamic-attribute/masquerade and contract inventories, validation-path agreement, closed error vocabulary, plugin provenance and catalog pins. New raw-option parsing must follow the existing Tier-3 boundary patterns. Do not add lint suppressions or hand-edit judge signatures.
- [ ] Establish test-capacity ownership and check host load before one broad suite. Use the canonical gate from the implementation worktree; it captures provenance, logs, exit codes, and tree stability:

```bash
cd "$(git rev-parse --show-toplevel)" && \
  scripts/full-suite-gate.sh --execute --detach \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer \
  --root "$PWD" \
  --log-dir "$PWD/.claude/lanes/composer-llm-contract-repair/final-gate"
```

- [ ] Read the emitted `summary.txt` after the `.done` marker exists. This change affects shared schema behavior and persisted review state, so both default pytest and serial PostgreSQL testcontainer coverage are required before integration. The explicit stage list is necessary: the script defaults to only Ruff and default pytest. If Docker is unavailable, run the other five stages explicitly and record the PostgreSQL stage as unmet. Compare the key-free trust-tier corpus against the baseline and leave operator signing separate.
- [ ] Re-verify HEAD and the owned changed-path set after gates. Commit the bounded implementation and tests, then hand off exact commits, focused/full-suite exits, live-provider status, and any remaining acceptance failures. Merge, restart, deployment, signing, and remote publication are separate delivery actions; do not infer them from a local green suite.
- [ ] If a regression is found after integration, revert the complete repair unit in a normal follow-up commit and rerun its acceptance tests. No database migration or bulk session rewrite is planned. Existing already-pending duplicate reviews must not be bulk-approved: attempt recovery only through valid stored authoritative evidence or normal user review.

## Review checkpoints

- [ ] All five numbered workstreams have an implementation owner, exact test location, and measurable completion condition.
- [ ] The source fix uses artifact-bound review reconciliation; a prompt-text hash is never substituted for source approval evidence.
- [ ] The generated-input guard is specific to this multi-query LLM node and preserves intentional upstream requirements and established input demotion behavior.
- [ ] The producer fix covers both single-query and multi-query successful output models without changing the global nullable-field policy.
- [ ] A green helper test is never used as proof of public tool behavior, a green graph test as proof of planner behavior, or a successful local run as proof of deployment.
