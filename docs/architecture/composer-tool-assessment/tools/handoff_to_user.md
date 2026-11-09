# `handoff_to_user` — terminal graph recovery and conversation handoff

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.
This is a reconnaissance assessment; no production change is proposed as already implemented.

## 1. Purpose and usage

Use this provider-authored terminal control when the user needs a question,
explanation, or a graph-only recovery action instead of further authoring in
the current turn. It ends authoring immediately. `park` retains the visible
draft and creates a bookmark; `reset_to_turn_start` targets the admitted starting
snapshot; `reset_to_safe_checkpoint` selects a locally editable checkpoint or
an empty workspace. “Safe” means representable in the editor, not approved,
valid, executable, or free of missing credentials.

It is available in both freeform and planner provider lists, and in the
recovery-only provider route used when the saved head cannot hydrate. It is
absent from the synchronous registry and Composer MCP. A live COMPOSE operation,
owned provider invocation, session authority, and remaining provider budget are
prerequisites; this is not a server-generated fallback around the provider.

## 2. Contract

Arguments are required `action` (`park`, `reset_to_turn_start`, or
`reset_to_safe_checkpoint`) and required `message` (1–16,000 characters,
nonblank after stripping); extra fields are forbidden. The owned intent also
requires a nonblank call identity of at most 1,024 characters. Credential material
in the message is refused. There are no action/message defaults.

The control must be the only tool call in its provider response. Parsing yields
an intent, not a committed graph or success receipt. Session settlement selects
a target and then atomically completes the operation with graph outcome,
assistant response, audit cohort and a trusted recovery notice. An unavailable
target produces `target_unavailable`; the saved assistant text becomes a trusted
refusal rather than the model's prospective success claim. The editor target
can still be invalid for execution. Reset does not delete prior states, revoke
external effects, undo blob writes, or refund provider spend.

There is no separate incremental proposal approval for this conversational
control. Execution and review admission remain independent. It is not cached.
Recovery-only mode makes one model call with the remaining deadline; planner
and freeform calls retain their enclosing budget/retry policies. Replay of an
already completed operation returns its durable response without new writes;
a fresh operation/call is not a globally idempotent command. Session fences,
base identity and final head comparison prevent stale recovery overwriting
another operation's work. Cancellation must settle audit and operation custody.

## 3. Implementation trace

- [Definition, strict argument models, wire projection, parser and singleton gate](../../../../src/elspeth/web/composer/recovery_controls.py#L17).
- [Freeform exposure](../../../../src/elspeth/web/composer/provider_gateway.py#L391)
  and [planner exposure](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647).
  Both dialects expose the same control names; strict transport omits string
  length keywords, which the server still enforces at admission.
- [Freeform recovery-only call and terminal result](../../../../src/elspeth/web/composer/service.py#L1028);
  [planner singleton rejection and `PlannerHandoff`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4351).
- [Owned intent and editable checkpoint qualification](../../../../src/elspeth/web/composer/graph_recovery.py#L39)
  deliberately avoid plugin construction and runtime validation.
- [Session admission](../../../../src/elspeth/web/sessions/service.py#L6607)
  binds scalar head identities before graph hydration;
  [target preparation](../../../../src/elspeth/web/sessions/service.py#L6701)
  verifies the COMPOSE fence, selects the snapshot and prepares settlement.
- [Turn settlement and trusted refusal](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)
  complete persistence; [browser operation decoder](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L151)
  checks receipt/message/state binding before presentation.

## 4. Alternative paths

Normal `park` can preserve an unpersisted but representable candidate. A malformed
head can use provider-selected reset without serializing that damaged candidate.
An empty session can reset to an empty authoring state. `reset_to_safe_checkpoint`
searches latest-first for an editable state: it may select the current draft
when that draft is editable, even if runtime validation rejects it. Turn-start
reset is the exact rollback choice when that is the user's intent.

Mixed recovery/discovery or recovery/mutation batches reject before dispatch.
Malformed/blank/credential-bearing arguments reject rather than recover. Missing
or unreadable explicit targets produce refusal. Provider unavailability,
expired budget, lost fences, mismatched base, audit failure or final head change
remain errors, not successful recovery. Completed-operation replay uses stored
response identity rather than reconstructing a new receipt.

## 5. Usability assessment

**Intended restriction, high confidence:** this restores editable graph state
only. Its name is broader than rollback semantics; callers must read the actual
receipt and not equate a terminal conversation with completed pipeline work.
Workaround for exact rollback is `reset_to_turn_start`; use preview and review
afterwards for readiness. No demonstrated integrity defect was found in the
focused recovery paths inspected and exercised.

**Source-established limitation, high confidence:** the advertised context lists
at most 100 recent parks, with no completeness/cursor indicator
([session query](../../../../src/elspeth/web/sessions/service.py#L6641)). This
does not delete older parks or prove a failed restore; it limits what the model
can discover. The practical impact above 100 parks remains unexercised.

## 6. Structural remediation

Preserve the existing intent → fenced settlement → receipt invariant. Integrate
recovery controls into a shared invocation descriptor that explicitly declares
their terminal status, argument contract, required authorities and receipt type;
do not route them through graph-mutation handlers merely to unify names. The
orchestrator should distinguish `conversation_handed_off`, `graph_recovered`,
and `pipeline_ready` in owned outcomes. Recovery/store ownership remains with
Sessions; plugin validation owns readiness, not checkpoint representability.

The same lifecycle distinction affects `restore_parked_graph`, advisor stops,
proposal terminals and user approval. A complete recovery catalogue needs
bounded paging or an explicit incomplete-result signal. These are contract
improvements, not authorization to migrate or erase existing history. Acceptance
must retain valid singleton recovery, invalid mixed-batch rejection, exact
rollback, empty fallback, unavailable-target refusal, fence/CAS rejection,
cancelled audit settlement and write-free completed replay.

## 7. Evidence, confidence, risk and limits

Read and executed meaningful bodies in
[checkpoint qualification tests](../../../../tests/unit/web/composer/test_graph_recovery.py),
[scripted planner recovery tests](../../../../tests/unit/web/composer/test_planner_graph_recovery.py),
[turn tests](../../../../tests/unit/web/sessions/test_graph_recovery_turn.py), and
[adversarial tests](../../../../tests/unit/web/sessions/test_graph_recovery_adversarial.py).
The parent focused selection also included strict dialect and planner protocol
tests: **86 passed, exit 0**. These prove local/scripted and SQLite behavior,
not remote-provider compliance, PostgreSQL contention, browser rendering or
deployment recovery. Existing browser decoder tests were located, not run.
Confidence in the described source/local contracts is high; the >100 park
experience is a source-established limitation needing a controlled boundary test.
Implementation risk is high if unification weakens fencing or receipt binding;
incremental descriptor adoption is reversible, persisted envelope changes need
explicit compatibility handling.

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | no |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.

This is the emitted recovery transport schema. It deliberately omits string length keywords; the semantic Pydantic parser still enforces the bounded/nonblank fields described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "additionalProperties": false,
  "description": "A terminal request, never an assertion that recovery already succeeded.",
  "properties": {
    "action": {
      "enum": [
        "park",
        "reset_to_turn_start",
        "reset_to_safe_checkpoint"
      ],
      "title": "Action",
      "type": "string"
    },
    "message": {
      "title": "Message",
      "type": "string"
    }
  },
  "required": [
    "action",
    "message"
  ],
  "title": "HandoffArguments",
  "type": "object"
}
```
