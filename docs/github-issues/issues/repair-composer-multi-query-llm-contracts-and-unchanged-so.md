---
title: Repair multi-query LLM contracts and unchanged-source approval retention
labels: [area/composer, type/task]
---

A user-reported workflow that classifies five colours using one LLM node with several queries is not authorable, valid and executable end to end. Four separate repairs are needed, across the planner's teaching surfaces, the LLM plugin's configuration guard, its output schema, and source approval retention.

## What happens

Context: the Composer is ELSPETH's web authoring interface, and a *multi-query LLM node* is one node that runs several named queries against a model and contributes each answer back as a field on the row. Working the reported workflow through the Composer surfaces four problems.

1. **Authoring guidance is wrong.** The input and output vocabulary taught to the planner misdescribes the node: input declarations should name upstream fields, and generated answers should be declared by the query output definitions.
2. **Generated outputs can be required as inputs to the same node.** Nothing rejects a configuration in which a field the node itself generates is also explicitly required as one of its inputs — a contradiction that cannot be satisfied.
3. **Successful outputs are typed as nullable.** Structured answers that are declared outputs and guaranteed present still carry an optional annotation. Measured on two fields of the reported pipeline:

   ```text
   good_colour_pair_answer: annotation=str | None; required=False;
                            declared_output=True; guaranteed=True
   approximate_hex_answer:  annotation=str | None; required=False;
                            declared_output=True; guaranteed=True
   ```

4. **Rebinding an unchanged source drops its approval.** A *route-only rebind* — pointing a source at the same content again without the content changing — moves an approved source back to pending. Exercised against a temporary session database and blob:

   ```text
   before_status: resolved
   after_status: pending
   after_existing_reconciler: resolved
   ```

   The approval is bound to `accepted_artifact_hash`, not to `resolved_prompt_template_hash`, and the restoration path is `reconcile_authoritative_reviews` (`src/elspeth/web/interpretation_state.py`). Supplying a current field value to the generic pending-requirement helper is not the repair.

## Where the work is

Items 1 and 2 span the Composer's planner teaching surfaces and plugin assistance, plus the LLM config models in `src/elspeth/plugins/transforms/llm/` (`multi_query.py` holds the query and output-field models, `validation.py` the config guards). Item 3 is in the same package's output-schema construction. Item 4 is in the source mutation path and `reconcile_authoritative_reviews`; the existing fixtures that build a matching approval proof are `TestEchoedServerOwnedMetadata` in `tests/unit/web/composer/test_promote_set_pipeline.py` and `tests/unit/web/composer/test_promote_set_source_from_blob.py`.

## Size and limits

This is large — four coordinated repairs plus the regression work that joins them, crossing plugin configuration, Composer admission and session persistence. It is not a good first ticket, but items 2 and 3 are separable and smaller than the rest.

Three limits to carry:

- The cross-boundary regression work that joins these repairs is **not** a fifth independently reproduced defect, and should not be tracked as one.
- A prompt-role validation and guidance change at `6211043b fix(composer): every llm node carries both prompt roles, enforced and always visible` post-dates the defective producer and source-rebind lines. Changed guidance may have exposed these paths, but that attribution is unproven without the original session trace or a controlled comparison between model providers. A rollback of that change is not the fix.
- The report also describes mutation during a bug-report request and a withheld final answer. The session concerned is absent from the local session database, so both need explicit acceptance checks; passing schema tests alone would not establish that they are repaired.

## Fix

The model provider stays responsible for authoring the graph — none of this may be worked around by generating pipeline structure server-side. Correct the planner's input and output vocabulary; reject contradictory declarations at both plugin configuration and Composer admission; make the output model describe successful rows with their actual non-null types and required presence; and restore source approvals through the existing reconciler rather than a new path.

Done looks like: the two measured blocks above invert. Declared, guaranteed answers report a non-optional annotation with `required=True`, and a route-only rebind of unchanged approved content leaves `after_status: resolved` without the reconciler having to repair it. Above that, the colour pipeline survives authoring, approval, repair, validation, persistence and execution in one integration test. Record the failing regressions before changing any production behaviour.

## Out of scope

One-off bug-report and read-only-mode changes.
