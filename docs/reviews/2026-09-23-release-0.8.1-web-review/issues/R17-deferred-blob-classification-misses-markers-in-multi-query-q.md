# R17. Deferred-blob classification misses markers in multi-query `queries.<name>.template`

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-10#2 |

## Finding

- **Status and severity:** confirmed, **medium**.
- **Location:** `src/elspeth/web/composer/state.py:2124-2129` (`ValidationProbeCache._construct`) and the pass-through arm at `state.py:5027-5039`.
- **What is wrong:** Deferral requires every pydantic error to be a marker `string_type` error. `LLMConfig.queries` is typed `list[QueryDefinition] | dict[str, QueryDefinition] | None` (`llm/base.py:216`). A marker in a query template therefore also produces a `list_type` or `dict_type` error from the other union branch, and that error's input is not a marker. The site is a legal ADR-034 prompt surface (`_validation_materialization.py:181-203`), and `wire_blob_inline_ref` writes nested paths.
- **Failure scenario:** A user-uploaded per-query prompt is wired into `queries.q1.template`. Stage 1 emits a high `contract_probe_failed` ("Check this node's plugin options") where it should emit a medium `contract_probe_deferred`. `docs/diagnostics/session-858953d1-repair.md` records this kind of misdiagnosis leading the planner to make unrelated option edits. 5bc4b56c9 fixed it only for top-level fields.
- **Suggested fix:** Evaluate errors per union branch, ignoring the other branch's container-type errors when one branch's only errors are marker `string_type` errors. Alternatively, substitute a probe-only placeholder at the admitted llm prompt sites (see R18).
- **Sources:** be-10#2.
- **Verifier notes:** Reproduced at the pin. The contract result is `(True, frozenset())` either way, so the harm is the misleading diagnosis. The list form is harder to reach than the dict form.


## Source findings and verification

### be-10#2: Deferred-blob classification misses markers in multi-query queries.<name>.template

- **Reported at:** `src/elspeth/web/composer/state.py:2126`; reviewer severity medium; category correctness; diff-anchored True.
- **Summary:** The deferral check in ValidationProbeCache._construct requires every pydantic error to be a marker string_type error. LLMConfig.queries is a list|dict union, so a marker in a query template also produces a list_type or dict_type error from the other union branch. As a result, an ADR-034-admitted per-query prompt marker is never deferred and is diagnosed as a failure in the node's plugin options.
- **Failure scenario:** A user-uploaded per-query prompt file is wired into queries.q1.template, a legal site listed by llm_prompt_surface_field. Stage 1 emits a high-severity contract_probe_failed warning telling the planner to "Check this node's plugin options", although the options are valid. docs/diagnostics/session-858953d1-repair.md records that this misdiagnosis led the planner to make unrelated option changes; 5bc4b56c9 fixed it only for top-level string fields.
- **Evidence:** Probe script against the pinned worktree. prompt_template marker: DEFERRED. system_prompt marker: DEFERRED. queries dict-form marker: "ValueError ... queries.list[QueryDefinition]: Input should be a valid list | queries.dict[str,QueryDefinition].q1.template: Input should be a valid string". queries list-form marker: "... Input should be a valid dictionary". Stage-1 validate(): "WARN contract_probe_failed high Computed contract probe for node 'classify' failed during preview (ValueError); ... Check this node's plugin options". _validation_materialization.py:190-201 admits queries.<name>.template as a prompt surface.
- **Suggested fix:** Evaluate the errors per union branch: ignore the other branch's container type errors when one branch's only errors are marker string_type errors. Alternatively, substitute a probe-only placeholder for valid inline_content markers at the admitted llm prompt sites (see the next finding).
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this finding. I traced the path at the pinned commit (HEAD 74c0ce0db336) and ran it. The deferral test in ValidationProbeCache._construct (state.py:2125-2129) needs every pydantic error to be a string_type error on an inline_content marker. LLMConfig.queries is declared as `list[QueryDefinition] | dict[str, QueryDefinition] | None` (plugins/transforms/llm/base.py:216). With the dict form, pydantic's union also reports a list_type error whose input is the whole queries dict. That input is not a marker, so all(...) is False and the call re-raises instead of deferring. Nothing upstream stops the marker. prepare_validation_probe_options (_validation_probe.py:24-80) only thaws, strips authoring keys and redacts secret refs; it never replaces inline markers. The marker can legitimately reach that position. llm_prompt_surface_field (web/execution/_validation_materialization.py:181-203) accepts queries.<name>.template as a guarded prompt surface. wire_blob_inline_ref writes nested option paths through _apply_inline_blob_marker/_set_nested_option (composer/tools/blobs.py:725+), and it refuses only LLM-authored blobs, so a user-uploaded blob is accepted there. llm is a known pass-through plugin, so the failure goes to the high-severity contract_probe_failed arm (state.py:5027-5039), whose message says "Check this node's plugin options". The deferred path (state.py:4993-5006) would have given a medium contract_probe_deferred warning instead. The contract result is the same either way (True, frozenset()), so the harm is the misleading diagnosis sent to the planner, not a wrong contract. Medium severity is fair. The list form is harder to reach, because _inline_content_marker_blob_ids does not walk lists, but the dict form is enough to confirm the defect. The tests added by 5bc4b56c9 (tests/unit/web/composer/test_deferred_blob_probe.py) only cover top-level markers, and I found no recorded ruling that excludes query templates.
