# R32. The `prompt_template_parts_required` guard is bypassed by echoing the unchanged parts, and the raw edit is silently dropped

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-11#1 |

## Finding

- **Location:** `src/elspeth/web/composer/tools/transforms.py:1658-1670` and `interpretation_state.py:3252-3260`.
- **Wrong:** The guard checks only whether the parts key is present. The reconciler then overwrites `prompt_template` with the render of the parts. **Scenario:** `patch_node_options {prompt_template: 'Totally new wording.', prompt_template_parts: <stored parts>}` returns success, the stored template is unchanged, and the approval digest is unchanged.
- **Fix:** Reject the patch when the patched `prompt_template` differs from the render of the merged parts.
- **Sources:** be-11#1 (probe `.claude/lanes/web-review-20260923/be-11-probe/probe.py`).
- **Verifier notes:** There is no audit breach, because what runs is what was approved. The defect is a false success report to the planner.


## Source findings and verification

### be-11#1: prompt_template_parts_required guard bypassed by echoing unchanged parts; raw prompt edit silently dropped with success

- **Reported at:** `src/elspeth/web/composer/tools/transforms.py:1663`; reviewer severity medium; category correctness; diff-anchored True.
- **Summary:** The guard checks whether the `prompt_template_parts` key is present in the patch, not whether the patched `prompt_template` matches what the merged parts render to. `_reconcile_node_options` always overwrites `prompt_template` with the parts render, and a structured node's review anchor is the parts skeleton hash. A patch that echoes the stored parts next to a new `prompt_template` therefore succeeds, and the edit is thrown away.
- **Failure scenario:** Structured approved LLM node; planner calls patch_node_options {prompt_template: 'Totally new wording.', prompt_template_parts: <stored parts verbatim>} -> success True, stored prompt_template remains 'Classify support categories for {{ row.text }}.', data None, approval digest unchanged; planner believes the prompt was edited and no review is reopened.
- **Evidence:** Probe be-11-probe/probe.py case 1 against pinned tree: '1 success True pt: Classify support categories for {{ row.text }}.' / '1 unchanged approval True'. The reconciler overwrites prompt_template at interpretation_state.py:3252-3260; the anchor is the structure hash (interpretation_state.py:2736-2763).
- **Suggested fix:** Reject when 'prompt_template' is in the patch and differs from the render of the merged parts, or reject any patch carrying prompt_template on a node whose merged options still have parts unless the value equals the stored one. Add a regression with the parts echoed.
- **Verifier (trace):** upheld, confidence high, severity low. I could not refute this. The guard at src/elspeth/web/composer/tools/transforms.py:1658-1670 (added in the window by 5bc4b56c9) rejects a changed `prompt_template` only when `PROMPT_TEMPLATE_PARTS_KEY not in patch`. It checks whether the key is present. It never compares the patched `prompt_template` with what the merged parts render to.

With the parts key present, the patch passes every later check in the handler:
- `_runtime_owned_llm_option_error`, the canonical-requirements check and the credential check.
- `_post_mutation_invariant_error`, which blocks only the codes in `_MUTATION_BLOCKING_INVARIANT_CODES` (_common.py:3695). None of those codes is about the prompt.
- The stored-digest check and `composition_review_contract_error`.

`reconcile_authoritative_reviews` then reaches `_reconcile_node_options` (src/elspeth/web/interpretation_state.py:3252-3260). Because `_prompt_parts(options)` is not None, it overwrites `options["prompt_template"]` with the render of the parts. When the parts are unchanged, the prompt review is carried over and `approved_prompt_artifact_hash` is recomputed to the same value. The tool therefore returns success with `data=None`, and the planner's new wording is silently thrown away.

The path is reachable. The per-turn context strips `prompt_template_parts` (prompts.py:310-323). The diagnostic `get_pipeline_state` view, however, emits options verbatim (_common.py:1655-1685), and the planner has its own earlier authoring in history. So a read-modify-write that echoes the parts alongside a new `prompt_template` is plausible.

This is not a recorded ruling. The only related ruling is recent-code-hints.md:1569, about echo-tolerant server-owned metadata, and it does not cover this. The window's own intent is the other way: the repair doc and the tool description say structured prompts must be edited through the parts and that the compiled template must not be changed alone. The only regression test, test_prompt_patch_reviews.py:165-176, covers only the patch without parts.

I lowered the severity from medium to low. There is no audit or integrity breach: the stored prompt and its approval stay consistent, so what runs is what was approved, and correctly no review is reopened. The harm is a false success on a mutation. The planner may tell the user the prompt changed. That is visible on the next state read, but the tool gives no signal.
