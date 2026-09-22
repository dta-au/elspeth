---
title: Repair multi-query LLM contracts and unchanged-source approval retention
labels: [area/composer, type/task]
---

A user-reported workflow that classifies five colours with one multi-query LLM node is not authorable, valid and executable end to end. Four repairs are needed across the planner's teaching surfaces, the LLM configuration guard, the producer output schema, and source approval retention.

## What happens

Working the reported workflow through the Composer surfaces four distinct problems:

1. **Authoring guidance.** The LLM input and output vocabulary taught to the planner is wrong: input declarations should carry upstream fields, and generated answers should be declared by the query output definitions.
2. **Generated outputs required as same-node inputs.** Nothing rejects a configuration in which a field the node generates is also explicitly required as an input to that same multi-query LLM.
3. **Nullable successful output contracts.** Structured answers that are declared outputs and guaranteed present still carry an optional, nullable annotation. Measured on two fields of the reported pipeline:

   ```text
   good_colour_pair_answer: annotation=str | None; required=False;
                            declared_output=True; guaranteed=True
   approximate_hex_answer:  annotation=str | None; required=False;
                            declared_output=True; guaranteed=True
   ```

4. **Unchanged source rebind loses approval.** A route-only rebind of source content that has not changed drops an existing approval. Exercised against a temporary session database and blob:

   ```text
   before_status: resolved
   after_status: pending
   after_existing_reconciler: resolved
   ```

   The approval is bound to `accepted_artifact_hash`, not to `resolved_prompt_template_hash`, and the correct restoration path is `reconcile_authoritative_reviews` — supplying a current field value to the generic pending-requirement helper is not the repair.

## Limits carried forward

The fifth workstream in the implementation plan is the cross-boundary regression and acceptance work that joins these repairs. It is **not** a claim of a fifth independently reproduced backend defect.

A prompt-role validation and guidance change at `6211043b` post-dates the defective producer and source-rebind lines. The changed guidance may have exposed these paths, but that causal attribution is unproven without the original session trace or a controlled provider comparison, and a rollback of that change should not be labelled the fix.

The report also describes mutation during a bug-report request and a withheld final answer. The session concerned is not present in the local session database, so those two symptoms need explicit acceptance checks; passing schema tests alone would not establish that they are repaired.

## Fix

The provider stays responsible for authoring the graph. Correct the planner's input and output vocabulary; reject contradictory declarations at both configuration and Composer admission; make the plugin's output model describe successful rows accurately; and restore source approvals through the existing authoritative review reconciler. Then exercise the whole workflow through real validation, persistence and execution boundaries, with failing regressions recorded before any production behaviour changes.

## Out of scope

One-off bug-report and read-only-mode changes.
