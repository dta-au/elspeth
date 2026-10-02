# R18. A deferred `llm` prompt marker leaves the node with no guarantees, so downstream required fields fail Stage 1

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-10#3 |

## Finding

- **Status and severity:** confirmed, **medium**. This is not a regression: the window relabels and codifies an existing fail-closed result.
- **Location:** `src/elspeth/web/composer/state.py:4994`, the `DeferredBlobContractProbe` arm.
- **What is wrong:** When an llm node's `system_prompt` or `prompt_template` is a valid user-uploaded `inline_content` marker, the new arm returns `(True, frozenset())`. The llm output contract does not depend on prompt text, as the `_validation_probe.py` docstring states, so the contract could be computed without the blob bytes. `state.py:3771-3778` already counts the same marker as "a real prompt".
- **Failure scenario:** A user uploads a prompt file as `prompt_template`, and the next step requires `[answer]`. Stage 1 always rejects with `guarantees: [(none)]`, even though the medium warning says execution validation will resolve it. The planner's only repairs are to drop the upload or drop the requirement. Run admission itself materialises blobs, so it is not blocked. The composer, however, shows the run as Failed and skips runtime preflight (`sessions/service.py:4651`).
- **Suggested fix:** At the admitted llm prompt sites only (`system_prompt`, `prompt_template`, `queries.<name>.template`), have `prepare_validation_probe_options` substitute an inert placeholder string for a valid marker. The placeholder is never persisted or sent, so this is not server-side authoring. Keep zero-guarantee deferral for plugins whose contract depends on the content, such as `reference_join`.
- **Sources:** be-10#3.
- **Verifier notes:** The deferral was designed and tested only for `reference_join` (`test_deferred_blob_probe.py`).


## Source findings and verification

### be-10#3: A deferred llm prompt marker leaves the node with no guarantees, so downstream required fields fail Stage 1

- **Reported at:** `src/elspeth/web/composer/state.py:4994`; reviewer severity medium; category correctness; diff-anchored True.
- **Summary:** The new DeferredBlobContractProbe arm in _effective_producer_vote_uncached returns (True, frozenset()). When an llm node's system_prompt or prompt_template is a user-uploaded inline_content marker, the node therefore guarantees nothing, and every downstream consumer with required fields is rejected. The llm output contract does not depend on prompt text (per the prepare_validation_probe_options docstring), so it could be computed without the blob bytes. This is not a regression: llm is a known pass-through plugin and already failed closed this way before the window, which relabels and codifies the result.
- **Failure scenario:** A user uploads a prompt file as an llm node's prompt_template (ADR-034, user modality). The next step declares required_input_fields: [answer], as the skill teaches, or a sink requires the field. Stage 1 rejects the pipeline with schema_contract_violation "guarantees: [(none)]" every time, even though the warning says execution validation will resolve it. The planner's only repair is to drop the uploaded prompt or the requirement.
- **Evidence:** Stage-1 script using the test_llm_producer_guarantees fixture shape. Plain llm plus field_mapper(required_input_fields=[answer]): valid=True. prompt_template marker: valid=False, "ERR schema_contract_violation ... Producer (llm) guarantees: [(none)]. Missing fields: [answer]" plus "WARN contract_probe_deferred medium". system_prompt marker: the same rejection. _validation_probe.py docstring: "The llm output contract is a pure function of the provider-INDEPENDENT config (schema block, response_field, queries/output_fields)". state.py:3771-3778 treats the same marker as "a real prompt".
- **Suggested fix:** At the admitted llm prompt sites only (system_prompt, prompt_template, queries.<name>.template), have prepare_validation_probe_options substitute an inert probe-only placeholder string for a valid inline_content marker, like the gateway provider stub. It is never persisted or sent, so this is not server authoring. Keep the zero-guarantee deferral for plugins whose contract depends on the content, such as reference_join.
- **Verifier (trace):** upheld, confidence high, severity medium. I could not refute this. I reproduced it at the pinned commit: an llm node whose prompt_template or system_prompt is a valid inline_content marker takes the new DeferredBlobContractProbe arm. That arm returns (True, frozenset()), and Stage 1 rejects any downstream consumer that requires a field. No guard stops the marker earlier. ADR-034 admits user-uploaded markers in llm prompt fields (tools/blobs.py:889). The prompt-role check counts a marker as a supplied prompt (state.py:3771-3778). The deferral is raised whenever every typed config error is a string_type error on an inline marker (state.py:2124-2129), and a marker in the llm prompt fields meets that condition. No ruling in docs/agents/recent-code-hints.md covers llm prompt markers. The deferral was designed and tested only for reference_join (test_deferred_blob_probe.py uses only _state_with_reference_join; docs/diagnostics/session-858953d1-repair.md describes only reference_join). The llm contract does not depend on prompt text (_validation_probe.py docstring), so the zero-guarantee result is a false rejection. As the finding itself says, this is not a regression: before the window, llm's known-pass-through fail-closed arm also returned zero guarantees. The window changes the label from a high contract_probe_failed to a medium contract_probe_deferred. That label still tells the user that execution validation will resolve it, but Stage 1 keeps failing the pipeline. One impact point the finding overstates: run admission (execution/service.py:1484 validate_pipeline) materializes the blobs and does not gate on Stage-1 state validity. Even so, the persisted composer validity stays invalid, runtime preflight is skipped (sessions/service.py:4651), the frontend shows the validation row as Failed (workspaceStatus.ts:65), and the planner gets an error it cannot fix. Medium severity stands.
