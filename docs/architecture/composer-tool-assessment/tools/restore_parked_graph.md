# `restore_parked_graph` — restore an exact parked draft

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.

## 1. Purpose and usage

Use this terminal provider control to resume an existing parked draft by its
server-advertised identity, then communicate with the user. It does not search
by title or approximate a past graph. It is exposed in freeform, planner and
malformed-head recovery routes, in both tool dialects. It has no synchronous
`ToolDeclaration` and no Composer MCP route. A live fenced COMPOSE operation,
provider authority, a session-scoped park identity and provider budget are
required. Restoration can be refused even though the model requested it.

## 2. Contract

Both fields are required: `park_id` is a nonblank string of 1–128 characters;
`message` is a nonblank string of 1–16,000 characters. Extra fields are rejected,
and credential material in the message is refused. No field has a default.
The control must be alone in its response. The owned intent adds the bounded
provider call identity and fixes action to `restore_parked_graph`.

Successful parsing creates an intent only. Session preparation looks up the
park by both park ID and current session, reads and qualifies its exact graph,
and prepares a new state identity with recovery provenance. Final operation
completion performs the write, preserves required evidence, and returns the
actual state plus trusted recovery notice. Missing, foreign or unrepresentable
targets become a `target_unavailable` refusal; the prospective model success
message is withheld in favour of trusted failure wording.

This has no discovery cache, no automatic handler retry, and no independent
proposal approval. The enclosing provider/operation lifecycle owns time,
quota, cancellation and replay. A completed operation replays without writes;
a new operation can restore the same snapshot again and create new provenance.
The COMPOSE fence and head comparison reject stale or foreign work. It restores
graph state only: no external-effect reversal, resource resurrection, credential
grant, execution approval, or unconditional validation success is implied.

## 3. Implementation trace

- [Definition and `RestoreArguments`](../../../../src/elspeth/web/composer/recovery_controls.py#L26),
  [strict local parsing and mixed-batch rejection](../../../../src/elspeth/web/composer/recovery_controls.py#L69).
- [Freeform provider list](../../../../src/elspeth/web/composer/provider_gateway.py#L391)
  and [planner list](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647).
  Strict wire schemas omit string length keywords; local Pydantic admission
  retains them.
- [Malformed-head provider route](../../../../src/elspeth/web/composer/service.py#L1028)
  supplies scalar identities, not damaged graph content;
  [planner handoff](../../../../src/elspeth/web/composer/pipeline_planner.py#L4351)
  terminates before candidate construction.
- [Recovery context/qualification](../../../../src/elspeth/web/composer/graph_recovery.py),
  [park advertisement](../../../../src/elspeth/web/sessions/service.py#L6641),
  [session-scoped lookup and qualification](../../../../src/elspeth/web/sessions/service.py#L6777).
- [Actual terminal settlement and refusal handling](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)
  and [browser receipt admission](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L151).

## 4. Alternative paths

A valid same-session park restores the exact editable snapshot. The current
head may be malformed without preventing the scalar recovery route. A park
from another session, unknown park, malformed target or credential-bearing
checkpoint is unavailable; nothing is reconstructed heuristically. Mixed
batches reject before discovery or mutation effects. Provider failure, budget
expiry, stale base, fence loss and failed persistence do not become success.
Completed replay returns the original durable terminal response even after
fence release. Cancellation tests cover settlement evidence independently of
the normal success response.

## 5. Usability assessment

**Intended restriction, high confidence:** no execution readiness is granted.
Missing files, resources, reviews or secrets can remain after a valid restore;
inspect/preview the result and obtain required review before execution.

**Source-established discoverability limitation, high confidence:** context
advertises at most 100 newest parks without a cursor or truncation flag. The
store lookup itself accepts an older known same-session identity, so do not
claim data loss or universal inability to restore. Model-directed selection of
an unadvertised older park lacks a first-class discovery route. No >100-park
interaction was executed during this assessment.

No demonstrated integrity defect was found in the inspected local recovery
paths. The current tests provide useful counterevidence to a hypothesis that a
model can force an arbitrary restore or publish success for a missing target.

## 6. Structural remediation

The orchestrator should receive an owned terminal outcome with requested action,
actual settlement/refusal, restored state identity and remaining readiness work.
A unified tool descriptor should carry async/terminal lifecycle and authority
requirements while leaving the atomic write with Sessions. This also affects
`handoff_to_user`, planner proposal completion and advisor termination.

Give park discovery explicit paging/completeness and same-session identities.
Preserve existing park IDs and stored state history; a new response envelope
must be versioned or adapted without weakening preserve/refuse behavior.
Implementation risk is high around stale-head and receipt semantics; a descriptor
change alone is reversible. Acceptance: known same-session exact restore;
unknown/foreign/corrupt target refusal; empty/oversize/extra argument rejection;
mixed batch produces no effects; changed head/fence loss rejects; cancelled
audit settles once; completed replay writes nothing; >100 parks are all
discoverable or explicitly marked incomplete.

## 7. Evidence, confidence, risk and limits

Inspected and ran
[planner recovery](../../../../tests/unit/web/composer/test_planner_graph_recovery.py),
[session turn recovery](../../../../tests/unit/web/sessions/test_graph_recovery_turn.py),
and [adversarial recovery](../../../../tests/unit/web/sessions/test_graph_recovery_adversarial.py)
within the parent's six-file selection: **86 passed, exit 0**. Meaningful
assertions include exact park identity, pre-dispatch mixed-batch rejection,
trusted missing-target refusal, malformed-head isolation, cancellation evidence
and completed replay with unchanged stored rows. These are scripted/local and
SQLite observations. PostgreSQL contention, live transports, large park history
and browser rendering were not exercised. Confidence is high for local/source
contracts; deployment portability needs its own required testcontainer gate
when implementation touches persistence or coordination.

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
  "properties": {
    "park_id": {
      "title": "Park Id",
      "type": "string"
    },
    "message": {
      "title": "Message",
      "type": "string"
    }
  },
  "required": [
    "park_id",
    "message"
  ],
  "title": "RestoreArguments",
  "type": "object"
}
```
