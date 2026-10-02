# Independent review: Composer guidance landing

Review snapshot: `docs/composer-guidance-landing` at `a4d3897da`, with uncommitted implementation diff as seen on 2026-09-29. Base for the implementation review is `9c2e8b17c`; current `release/0.8.1` has advanced to `63906a374` with a CI change. This is the one independent read-only review pass requested by the landing prompt.

## Finding

### P2 — T-01 created-output rule is contradicted by preview repair messages

- **Location:** `src/elspeth/web/composer/tools/generation.py:3107-3110` and `:3238-3239`.
- **Evidence:** The aggregation numeric-field repair says a `type_coerce (or value_transform) node's own schema: block declares what ARRIVES at the node ... never the transformed result`; the generic source/consumer repair says `A node's own schema: block declares what ARRIVES at the node, never what it should be converted to.` The first statement also treats value_transform like a converter whose output is derived from `conversions`, although `conversions` is a type_coerce option. Both strings are returned as `suggested_repair` from `_blocking_diagnostic` in `preview_pipeline` and therefore reach the planner. Current T1 behavior derives a value_transform target output type from its expression and permits an authored created-target output type; a provably incompatible authored target is refused at build (work-log CLI probe `t1-expression/provable-bad.yaml`). The changed `tools/transforms.py:277-282` and `pipeline_capabilities.md:279-286` teach this correctly.
- **Impact:** The planner may remove or misplace a valid created-target type declaration while repairing a different arriving-field mismatch. This preserves the exact contradiction that T-01 is meant to remove on an active feedback surface.
- **Repair:** Scope both diagnostics to the particular consumed field that arrives from observed CSV as `str`. State that a type_coerce conversion determines the output type and that a value_transform created target can have an authored or provable output type. Do not make an absolute claim about every field in a node schema.

## Checked without additional findings

- Cross-checked the in-scope triage table against the 09-27 review, Astra amendments, SLOTTING and the updated skills, aids, plugin hints, catalogue projections, tool responses, and refusal text. The `row | items | list`, carrier-qualified header alias, retired `field_mapper.strict`, R2 missing-field route, T1 typing, and batch capability wording are consistent with the recorded absolute-settings CLI controls in `work-log.md` and the corresponding code paths. I did not start another provider-backed run during this pass.
- T-04 is class-derived from `is_batch_aware` and `flush_emits_one_row_per_buffered_row` in `web/catalog/service.py:309-317`; the new field is carried through inventory, selected-schema snapshot, planner contract, schema evidence, and discovery digest. P-14's configured-instance projection is explicitly deferred under the prompt's small-code stop rule; P-18's digest directs the planner to the chosen plugin's full schema for enum values.
- The `union_field_collision` addition is an exact runtime-reason branch in `explain_validation_error`, without a fabricated build-time code or composer-authored collision policy. No server-authored pipeline path or tutorial-specific path appeared in the diff.
- `git diff --check` exited 0. Generated golden, parity, hash, corpus, and census procedures are described in `work-log.md`; I did not independently rerun every generator, the broad focused suite, or the full gate. Those remain landing gates for the main agent.

## Integration note

`release/0.8.1` advanced from the branch base `9c2e8b17c` to `63906a374`. Merge that tip into the branch and assess its reached tests before the local fast-forward. This is branch drift, not a finding against the guidance patch.
