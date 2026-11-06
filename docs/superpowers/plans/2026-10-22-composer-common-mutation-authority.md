# Composer Common Mutation Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve request-start and commit-time trust authority on the common Composer path before retiring `PlanningApplication` in H2.3.

**Architecture:** A nominal, immutable request-start ceiling limits every iteration; the current preference still decides whether an actual provider-authored mutation is proposed or dispatched. The existing session transaction and blob custody owners enforce that ceiling and current trust at business-effect boundaries. P4 publishes the actual committed or refused outcome together with the actual tool evidence, without manufacturing a proposal or replaying a tool.

**Tech Stack:** Python frozen dataclasses, SQLAlchemy, existing session-operation CAS and required-work workers, SQLite, PostgreSQL, FastAPI, TypeScript codecs, pytest and Vitest.

---

## Scope, source and prerequisites

This is a proposed implementation packet, not an implementation or test receipt.
Source inspection used the integration checkout, HEAD
`51bb8131723baa822c1ce65ce1a981722d6c90f6`, with the index matching tree
`37e0b0a4e767dd18c18aa7b3f15a965ca4ff43b5` at inspection. Commands and raw output:

```text
git rev-parse HEAD
51bb8131723baa822c1ce65ce1a981722d6c90f6
git diff --cached --name-only 37e0b0a4e767dd18c18aa7b3f15a965ca4ff43b5
(no output)
git check-ignore -v docs/superpowers/plans/2026-10-22-composer-common-mutation-authority.md
(no output)
```

`AGENTS.md`, the applicable whole-tree/locking/test sections of
`CONTRIBUTING.md`, the actual owners below, and the source review
`.claude/lanes/first-carry-20261021/h2-common-trust-review.md` were read.
No application import, test collection/execution, provider call, schema change,
source/test edit, installation, service action, account/key change, stage or
commit is part of this planning task. The sole durable output is this plan.

H2.1's common entry and H2.2's authenticated full-history adapter remain.
The existing facade-retirement owner retains H2.3 ownership. This packet is a
prerequisite, not permission to delete or rewrite the legacy test modules.
The separate [G3 monetary-continuation packet](2026-10-22-composer-common-provider-cost.md) must also pass before
H2.3: preserve `composer_planner_max_cumulative_provider_cost`, unknown-cost
refusal, covered-call scope and the original monetary ledger semantics.
Token quota is not monetary evidence. Neither this plan nor H2.3 approves a
weaker monetary policy. The coordinator owns that companion and its exact
integration review; the authority architecture below does not depend on a
monetary-policy choice.

Compatibility decision for new .10 requests: common auto mode may produce
actual direct graph checkpoints and tool audit instead of the old automatic
proposal history. Explicit mode still creates a real pending proposal.
Historical rows, existing proposal settlement and in-flight replay remain
readable; no migration, reset, downgrade or invented `pipeline_commit_intent`.
The direct `pipeline_planner` library remains supported and tested.

### Verified seams

| Source in inspected tree | Actual behavior to preserve or repair |
| --- | --- |
| `composer/service.py:1179–1245,1537–1646,2303–2347` | Request enters one loop. Current preferences are read in `_dispatch_tool_batch` after the provider. Capture the initial ceiling before entering this loop. |
| `composer/tool_batch.py:1038–1054,1728–1737` | Proposal cap and dispatch each independently use current preference; both must consume the same effective decision. |
| `composer/tool_batch.py:2133–2174` | Actual explicit `set_pipeline` proposal owner. Early mismatch uses this branch. |
| `composer/turn_audit.py:396–421,493–526,578–639` | P4 prepares visible/private rows, writes through the real required SQL ticket, then projects actual checkpoint evidence. |
| `sessions/service.py:2018–2104,2340–2409` | Locked graph-head CAS and graph inserts; current trust is absent. The actual worker is joined on cancellation. |
| `sessions/service.py:2931–3030,3708–3767` | Preference writer shares session lock; old proposal settlement already reads trust inside its business transaction. |
| `composer/tools/sessions.py:2069–2106` | Inline blob allocation happens before P4 graph persistence. |
| `composer/tools/blobs.py:1498–1538,1727–1765,1826–1856` | Create custody, replacement coordinator, deletion ledger are actual effect owners. |
| `blobs/service.py:2693–2802,3024–3050,3118–3235`; `blobs/replacement.py:229–340` | Existing staged filesystem/SQL custody and reconciliation; do not replace with ad-hoc file writes. |
| `coordination/repository.py:5443–5474`; `sessions/locking.py` | Exact operation CAS and session lock. Blob custody uses its own PostgreSQL advisory class; preference writer must join that existing custody lock before its existing session transaction. |
| `composer/service.py:188–259,2744–2798,2826–3000` | Join started P3/P4, retain required audit, record actual saved state, then re-raise cancellation/failure. |

## A1 — Request ceiling and one effective dispatch decision

**Create:** `src/elspeth/contracts/composer_mutation.py`.
**Modify:** `src/elspeth/web/composer/service.py`, `tool_batch.py`,
`_compose_loop_carriers.py`, `tools/_common.py`, `tools/_dispatch.py`.
**Test:** `tests/unit/web/composer/test_common_mutation_authority.py`.

- [ ] Add the following owned types. The ceiling is historical restriction,
  never a reusable fence. `request_id` is the original logical user-message
  identity, not a provider call ID or cold worker's operation epoch.

```python
from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind

type MutationTrustMode = Literal["auto_commit", "explicit_approve"]

def _valid_trust_mode(value: object) -> bool:
    return value in ("auto_commit", "explicit_approve")

@dataclass(frozen=True, slots=True)
class ComposerMutationCeiling:
    session_id: str
    request_id: str
    initial_trust_mode: MutationTrustMode

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("Mutation ceiling requires its session identity")
        if type(self.request_id) is not str or not self.request_id:
            raise ValueError("Mutation ceiling requires its logical request identity")
        if not _valid_trust_mode(self.initial_trust_mode):
            raise ValueError("Mutation ceiling has an invalid initial trust mode")

    def validate_context(self, context: SessionOperationContext) -> None:
        if (type(context) is not SessionOperationContext
                or context.operation_kind is not SessionOperationKind.COMPOSE
                or context.fence.session_id != self.session_id):
            raise AuditIntegrityError("Mutation ceiling differs from current COMPOSE custody")

    def permits_auto(self, current: MutationTrustMode) -> bool:
        if not _valid_trust_mode(current):
            raise AuditIntegrityError("Session has an invalid trust mode")
        return self.initial_trust_mode == "auto_commit" and current == "auto_commit"

@dataclass(frozen=True, slots=True)
class ComposerMutationRevocation:
    ceiling: ComposerMutationCeiling
    current_trust_mode: MutationTrustMode

    def __post_init__(self) -> None:
        if type(self.ceiling) is not ComposerMutationCeiling:
            raise TypeError("Mutation revocation requires an owned ceiling")
        if self.ceiling.permits_auto(self.current_trust_mode):
            raise ValueError("Allowed mutation cannot be reported as revoked")

class ComposerAutoMutationRefused(RuntimeError):
    def __init__(self, revocation: ComposerMutationRevocation) -> None:
        if type(revocation) is not ComposerMutationRevocation:
            raise TypeError("Mutation refusal requires an owned revocation")
        self.revocation = revocation
        super().__init__("Automatic mutation authority is no longer available for this request.")
```

- [ ] Add `mutation_ceiling: ComposerMutationCeiling | None = None` as an explicit
  keyword argument to public `compose`, and the explicit parameter
  `mutation_ceiling: ComposerMutationCeiling | None` to `_compose_loop`, `_dispatch_tool_batch`,
  `_persist_turn_audit` and module `persist_turn_audit`. Add the field to
  `ToolBatchContext` and `ToolContext`; add the optional keyword to
  `execute_tool`, forwarding it into its `ToolContext` construction.
  In `sessions/composer_turn.py::_run_composer_turn`, after the actual original
  user-message ID is known and before the E14/F-C5
  `compose_budget_seconds = budget_anchor.remaining_seconds(...)` measurement
  at line 1405 (not merely before `if should_auto_title`),
  capture preferences once using `service.get_composer_preferences(turn.session_id)`.
  Construct the ceiling with `str(turn.session_id)` and
  `str(turn_user_message_id)`, and pass it into `composer.compose` at line 1486.
  This is before TITLE as well as PRIMARY. In direct `compose` calls with no
  supplied ceiling, after custody checks and before chargeable admission,
  capture preferences once using `get_composer_preferences(UUID(session_id))`.
  Construct the ceiling with `user_message_id` or the once-created
  `TurnEvidence.request_id`. Move that request-ID allocation before the read,
  and reuse it in `TurnEvidence`; never allocate a second logical ID.

```python
logical_request_id = user_message_id or str(uuid4())
if session_id is not None and mutation_ceiling is None:
    initial_preferences = await self._require_sessions_service().get_composer_preferences(UUID(session_id))
    mutation_ceiling = ComposerMutationCeiling(
        session_id, logical_request_id, initial_preferences.trust_mode
    )
if mutation_ceiling is not None:
    if (type(mutation_ceiling) is not ComposerMutationCeiling
            or mutation_ceiling.session_id != session_id
            or mutation_ceiling.request_id != logical_request_id):
        raise AuditIntegrityError("Compose received another logical request's mutation ceiling")
```

  Pass the exact object to `_compose_loop`, all iterations, P3 and P4. A
  session-bound loop must reject a missing ceiling; sessionless trained-
  operator behavior remains sessionless. `_run_one_turn_for_test` must obtain
  the actual fixture's session preferences before entering the loop, rather
  than defaulting to auto. Current cold-resume integration is specified in A6.

- [ ] In `_dispatch_tool_batch`, retain the existing current preference read.
  Compute once and add `review_required: bool` to `ToolBatchContext`:

```python
review_required = False
if turn_session_uuid is not None:
    if type(mutation_ceiling) is not ComposerMutationCeiling or turn_preferences is None:
        raise AuditIntegrityError("Session mutation dispatch lost its request ceiling")
    if mutation_ceiling.session_id != str(turn_session_uuid):
        raise AuditIntegrityError("Mutation dispatch crossed sessions")
    review_required = not mutation_ceiling.permits_auto(turn_preferences.trust_mode)
```

  Replace only the current-mode predicates in the proposal-cap and
  `explicit_approval_required` expressions with `ctx.review_required`.
  Preserve the existing mutation/tool-classification predicates exactly.
  `create_blob` remains immediate. `update_blob`, `delete_blob`, and all graph
  mutations continue to require review when effective authority is explicit.
  Pass `mutation_ceiling` to direct approval-governed tool execution only;
  explicit proposal custody and immediate `create_blob` pass `None` to the
  business-write guard. Keep the request ceiling itself in P4 even for review.

- [ ] Add these unit tests before implementation and run them RED, then GREEN:

```python
import pytest

from elspeth.contracts.composer_mutation import ComposerMutationCeiling

@pytest.mark.parametrize("initial,current,allowed", [
    ("auto_commit", "auto_commit", True),
    ("auto_commit", "explicit_approve", False),
    ("explicit_approve", "auto_commit", False),
    ("explicit_approve", "explicit_approve", False),
])
def test_request_ceiling_intersects_current_trust(initial, current, allowed):
    ceiling = ComposerMutationCeiling("session", "request", initial)
    assert ceiling.permits_auto(current) is allowed
    assert ceiling.initial_trust_mode == initial
```

  Add a real over-cap common batch starting explicit and upgraded while the
  primary call is held: it must reach the existing proposal cap, with zero
  proposals and zero graph effects. A current-only mutant must fail that test.

### A1 exact interface edits

Apply these contextual edits after adding the A1 types. Each added name has
its definition in A1/A3; import `ComposerMutationCeiling`,
`ComposerMutationRevocation`, and `ComposerAutoMutationRefused` from
`elspeth.contracts.composer_mutation` in the files using those names.
The `compose` protocol receives the identical final keyword as its concrete
implementation, preserving all existing positional parameters.

```diff
*** Update File: src/elspeth/web/composer/protocol.py
@@
         advisor_history: AdvisorTranscriptHistory | None = None,
+        *,
+        mutation_ceiling: ComposerMutationCeiling | None = None,
     ) -> ComposerResult:
*** Update File: src/elspeth/web/composer/service.py
@@
         advisor_history: AdvisorTranscriptHistory | None = None,
+        *,
+        mutation_ceiling: ComposerMutationCeiling | None = None,
     ) -> ComposerResult:
@@
-                request_id=user_message_id or str(uuid4()),
+                request_id=logical_request_id,
```

In the call to `_compose_loop` at line 1225, insert
`mutation_ceiling=mutation_ceiling,` immediately before its `turn_evidence`
keyword. The explicit signature/call additions for the remaining service
seams are these complete parameter blocks (they do not replace any existing
parameters):

```python
# _compose_loop keyword-only signature, immediately after policy_catalog:
mutation_ceiling: ComposerMutationCeiling | None,
# _dispatch_tool_batch keyword-only signature, immediately after call_model:
mutation_ceiling: ComposerMutationCeiling | None,
# _persist_turn_audit keyword-only signature, immediately after tool_outcomes:
mutation_ceiling: ComposerMutationCeiling | None,
authority_revocation: ComposerMutationRevocation | None,
base_state: CompositionState,
# module turn_audit.persist_turn_audit after tool_outcomes:
mutation_ceiling: ComposerMutationCeiling | None,
authority_revocation: ComposerMutationRevocation | None,
base_state: CompositionState,
```

```python
# service._compose_loop -> self._dispatch_tool_batch at line 2676:
mutation_ceiling=mutation_ceiling,
# service._dispatch_tool_batch -> ToolBatchContext after turn_preferences:
mutation_ceiling=mutation_ceiling,
review_required=review_required,
# service._compose_loop -> self._persist_turn_audit at line 2778:
mutation_ceiling=mutation_ceiling,
authority_revocation=dispatch_result.authority_revocation,
base_state=_state,
# service._persist_turn_audit -> module persist_turn_audit at line 1513:
mutation_ceiling=mutation_ceiling,
authority_revocation=authority_revocation,
base_state=base_state,
```

Exact carrier/context and execution edits:

```diff
*** Update File: src/elspeth/web/composer/tool_batch.py
@@
     turn_preferences: ComposerSessionPreferencesRecord | None
+    mutation_ceiling: ComposerMutationCeiling | None
+    review_required: bool
     cancellation_requested: asyncio.Event
@@
     advisor_calls_used: int
+    authority_revocation: ComposerMutationRevocation | None = None
*** Update File: src/elspeth/web/composer/tools/_common.py
@@
     executing_proposal_id: str | None = None
+    mutation_ceiling: ComposerMutationCeiling | None = None
     _interpretation_requirements_are_internal: bool = False
*** Update File: src/elspeth/web/composer/tools/_dispatch.py
@@
     executing_proposal_id: str | None = None,
+    mutation_ceiling: ComposerMutationCeiling | None = None,
     validate_arguments: bool = False,
@@
         executing_proposal_id=executing_proposal_id,
+        mutation_ceiling=mutation_ceiling,
         _interpretation_requirements_are_internal=_interpretation_requirements_are_internal,
```

In `tool_batch.py` replace the full cap outer condition with
`if ctx.review_required:`; preserve its count body. Replace the whole routing
expression with this exact expression:

```python
explicit_approval_required = (
    ctx.review_required
    and is_mutation_tool(tool_name)
    and (not is_blob_store_only_mutation_tool(tool_name)
         or is_approval_required_blob_store_only_mutation_tool(tool_name))
)
auto_mutation = (
    ctx.mutation_ceiling
    if (not explicit_approval_required and is_mutation_tool(tool_name)
        and (not is_blob_store_only_mutation_tool(tool_name)
             or is_approval_required_blob_store_only_mutation_tool(tool_name)))
    else None
)
```

Add `mutation_ceiling=auto_mutation` to the direct candidate `ToolContext`
at line 1753. Add `_auto_mutation: ComposerMutationCeiling | None = auto_mutation`
to `_do_dispatch`'s default-bound arguments. Add
`mutation_ceiling=_auto_mutation` to its `execute_tool` worker keywords.
Wrap exactly that await, retaining all its existing arguments, in this handler:

```python
except ComposerAutoMutationRefused as exc:
    acc.authority_revocation = exc.revocation
    return _failure_result(
        _state, str(exc), error_code="auto_mutation_revoked",
        with_state_validation=False,
    )
```

The `try` covers only `run_sync_in_worker`, so this handler cannot convert
later finalization errors into authority refusals. Add the explicit carrier
fields at the end of `_DispatchOutcome` and `_PersistOutcome`:

```python
authority_revocation: ComposerMutationRevocation | None = None
```

The `_run_one_turn_for_test` helper's state setup already resolves
`resolved_session_id`; immediately before its `_compose_loop` call use:

```python
test_preferences = await self._require_sessions_service().get_composer_preferences(UUID(resolved_session_id))
test_ceiling = ComposerMutationCeiling(
    resolved_session_id, str(uuid4()), test_preferences.trust_mode
)
```

Pass `mutation_ceiling=test_ceiling` in that call. Its fixture must create the
real session it names; a nonexistent default UUID must fail as missing fixture
state, not manufacture auto authority. For direct `_compose_loop` tests, the
same nominal ceiling comes from their actual seeded session preferences.

In `_run_composer_turn`, insert the following before the existing E14/F-C5
remaining-budget measurement. The existing measurement and nonpositive-budget
branch then execute after this await, for both auto-title choices; do not pass
a pre-read stale remaining budget to `compose`. The capture remains inside the
existing outer turn audit/settlement custody. Cost-policy capture is synchronous
and occurs before that same final measurement (see G3).

```python
initial_preferences = await service.get_composer_preferences(turn.session_id)
mutation_ceiling = ComposerMutationCeiling(
    str(turn.session_id), str(turn_user_message_id), initial_preferences.trust_mode
)
```

```python
# composer.compose call at line 1486:
mutation_ceiling=mutation_ceiling,
```

The existing explicit test adapter must accept the new production keyword;
otherwise the HTTP integration tests stop before reaching any authority code:

```diff
*** Update File: tests/helpers/composer_lease.py
@@
         advisor_history=None,
+        mutation_ceiling=None,
     ):
@@
             "advisor_history": advisor_history,
+            "mutation_ceiling": mutation_ceiling,
```

## A2 — Locked business predicate and blob custody

**Modify:** `src/elspeth/web/coordination/repository.py`,
`src/elspeth/web/sessions/protocol.py`, `sessions/service.py`,
`src/elspeth/web/blobs/service.py`, `blobs/replacement.py`,
`composer/tools/blobs.py`, `composer/tools/sessions.py`.
**Create:** `src/elspeth/web/coordination/composer_mutation_guard.py`.

- [ ] Add the shared connection-local guard below. The caller must already
  hold the existing session transaction lock. This performs no await, starts
  no second transaction and does not replace CAS/job/Stop/deadline checks.

```python
from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.composer_mutation import (
    ComposerAutoMutationRefused, ComposerMutationCeiling, ComposerMutationRevocation,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.sessions.models import sessions_table

def check_auto_mutation_on_connection(
    conn: Connection, *, context: SessionOperationContext,
    ceiling: ComposerMutationCeiling,
) -> ComposerMutationRevocation | None:
    if type(ceiling) is not ComposerMutationCeiling:
        raise TypeError("Auto mutation requires an owned ceiling")
    ceiling.validate_context(context)
    current = conn.execute(select(sessions_table.c.trust_mode).where(
        sessions_table.c.id == ceiling.session_id
    )).scalar_one()
    if current not in ("auto_commit", "explicit_approve"):
        raise AuditIntegrityError("Session trust mode is corrupt")
    return None if ceiling.permits_auto(current) else ComposerMutationRevocation(ceiling, current)

def require_auto_mutation_on_connection(
    conn: Connection, *, context: SessionOperationContext,
    ceiling: ComposerMutationCeiling,
) -> None:
    refused = check_auto_mutation_on_connection(conn, context=context, ceiling=ceiling)
    if refused is not None:
        raise ComposerAutoMutationRefused(refused)
```

- [ ] Extend both protocol and repository `mutate` signatures with the
  keyword-only parameter `auto_mutation: ComposerMutationCeiling | None = None`.
  After `_compare_and_swap_on_connection` and
  `require_composer_operation_mutation_on_connection`, and before constructing
  `_RepositoryMutationTransaction`, add:

```python
if auto_mutation is not None:
    require_auto_mutation_on_connection(conn, context=context, ceiling=auto_mutation)
```

  The new guard applies only when an actual auto business caller supplies it.
  Audit/proposal writes, renewal, read custody and cleanup must still work in
  explicit mode. Do not introduce a global COMPOSE trust check.

- [ ] Extend these existing signatures with the same optional keyword
  `auto_mutation: ComposerMutationCeiling | None = None`, preserving every
  existing parameter and return type:

| Callee | Exact callers and forwarding |
| --- | --- |
| `_persist_prepared_blob_create` | `_execute_set_pipeline` passes `context.mutation_ceiling`; `_execute_create_blob` deliberately passes `None`; proposal custody calls remain `None`. |
| `_persist_blob_content` | `_persist_prepared_blob_create` forwards `auto_mutation`; all existing non-auto callers remain `None`. |
| `BlobServiceImpl._persist_fenced_blob_record` | `_persist_blob_content` forwards it in the exact-context arm. |
| `BlobReplacementCoordinator.replace_blob` | `_execute_update_blob` passes `context.mutation_ceiling`; accepted-proposal callers remain `None`. |
| `BlobServiceImpl._delete_blob_with_ledger` and `_commit_blob_deletion_plan` | `_execute_delete_blob` passes `context.mutation_ceiling`; the ledger driver forwards it to the commit owner; authenticated API and accepted-proposal callers remain `None`. |

  In creation, supply `auto_mutation` to `mutate` calls for `reserve_blob` and
  `mark_blob_ready`. In replacement supply it to `prepare_blob_replacement`,
  `mark_blob_replacement_staged` and `commit_blob_replacement`. In deletion
  supply it to `prepare_blob_deletion` and the actual commit mutation in
  `_commit_blob_deletion_plan`. Reads and abort/discard/reconcile/purge retain
  their existing operation authority and do not require current auto mode.
  Preserve uncertain-commit reconciliation: a committed ready/replacement/
  deletion result remains committed even when the return/acknowledgement fails.

- [ ] Put the preference writer's existing transaction inside the existing
  blob custody scope, in this order. Do not take the route compose lock, hold
  this across providers, or acquire custody after opening the session SQL lock.

```diff
*** Update File: src/elspeth/web/sessions/service.py
@@
         def _sync() -> ComposerSessionPreferencesTransition:
-            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
+            with (
+                _blob_custody_session_lock(self._engine, sid),
+                self._session_process_locked_begin(sid) as conn,
+                self._session_write_lock(conn, sid),
+            ):
```

  The complete existing transaction body stays at its existing indentation.
  Existing creation/replacement/deletion
  drivers already hold custody across their filesystem phases. Consequently
  a preference downgrade either precedes their guarded SQL and refuses the
  business write, or follows their completed custody operation. The session
  transaction still checks actual current mode atomically with each business
  write. Renewal uses the separate sessions advisory class and remains live.
  Do not add nested PostgreSQL custody acquisition to the same effect path:
  that helper uses a separate connection and is not reentrant across backends.

- [ ] Handle `ComposerAutoMutationRefused` inside `_do_dispatch` around the
  actual `run_sync_in_worker(execute_tool, ...)` await, before
  `dispatch_with_audit` sees it. Assign its nominal revocation into a new
  `BatchAccumulator.authority_revocation` field and return the existing
  `_failure_result(_state, str(exc), error_code="auto_mutation_revoked",
  with_state_validation=False)`. The normal invocation recorder now records
  an actually returned tool refusal, not a plugin crash or argument error.
  Append that real outcome and break the batch; no subsequent tool starts.
  Copy the field into `_DispatchOutcome.authority_revocation`.
  Skip new EARLY work when that field is set; always execute P4.

  Place the batch stop after the ordinary result/outcome/tool-message append
  and mutation/discovery classification, immediately before the existing
  `preview_source_proof_stop` check at `tool_batch.py:3390`:

```python
if acc.authority_revocation is not None:
    break
```

  The new `_DispatchOutcome` construction passes
  `authority_revocation=acc.authority_revocation`. The EARLY condition adds
  `and dispatch_result.authority_revocation is None`. Both newly added
  dataclass fields default to `None`; the accumulator is request-local.

  If a prior inline/blob effect committed while authority was valid, retain
  its actual invocation result and private evidence. Do not delete a ready
  blob to make a later graph refusal appear effect-free. Pending cleanup uses
  existing reconciliation; no new chmod/chown, accounts, locks or keys.

### A2 exact custody signatures and business calls

The following complete signature replacements preserve existing bodies.
Import `ComposerMutationCeiling` from the new contract owner in each affected
module. The repository implementation and protocol share the same keyword.

```python
# sessions.protocol.SessionOperationAuthority and repository implementation:
def mutate[T](
    self, context: SessionOperationContext,
    mutation: Callable[[SessionOperationMutationTransaction], T], *,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> T:
```

```python
# composer.tools.blobs:
def _persist_prepared_blob_create(
    prepared: _PreparedBlobCreate, *, session_engine: Engine, session_id: str,
    max_blob_storage_per_session_bytes: int | None = None,
    session_operation_context: SessionOperationContext | None = None,
    session_operation_authority: SessionOperationAuthority | None = None,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> str | None:

# blobs.service:
def _persist_blob_content(
    *, engine: Engine, data_dir: Path, max_storage_per_session: int,
    blob_id: UUID, session_id: UUID | str, filename: str, content: bytes,
    mime_type: StorageMimeType, created_by: BlobCreator,
    source_description: str | None, creation_modality: CreationModality,
    created_from_message_id: str | None, creating_model_identifier: str | None,
    creating_model_version: str | None, creating_provider: str | None,
    creating_composer_skill_hash: str | None, creating_arguments_hash: str | None,
    idempotent: bool, fork_write_fence: BlobForkWriteFence | None = None,
    write_guard: Callable[[], None] | None = None,
    session_operation_authority: SessionOperationAuthority | None = None,
    session_operation_context: SessionOperationContext | None = None,
    quota_exceeded_recorder: Callable[[QuotaExceeded], None] = refuse_unrecorded_quota_exceeded,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> Row[Any] | BlobRecord:

def _persist_fenced_blob_record(
    self, reservation: BlobRecord, content: bytes,
    session_operation_context: SessionOperationContext, *, idempotent: bool,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> BlobRecord:

def _delete_blob_with_ledger(
    self, *, blob_id: UUID, context: SessionOperationContext,
    accepting_proposal_id: UUID | None = None,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> None:

def _commit_blob_deletion_plan(
    self, *, context: SessionOperationContext, plan: BlobDeletionPlan,
    accepting_proposal_id: UUID | None = None,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> BlobDeletionPlan:

# blobs.replacement.BlobReplacementCoordinator:
def replace_blob(
    self, *, expected: BlobRecord, replacement: BlobRecord, content: bytes,
    context: SessionOperationContext, max_storage_per_session: int,
    accepting_proposal_id: UUID | None,
    auto_mutation: ComposerMutationCeiling | None = None,
) -> BlobRecord:
```

Add these exact keyword arguments to the named existing calls; no positional
argument or closure binding changes:

```python
# tools.sessions._execute_set_pipeline -> _persist_prepared_blob_create:
auto_mutation=context.mutation_ceiling,
# tools.blobs._persist_prepared_blob_create -> _persist_blob_content:
auto_mutation=auto_mutation,
# blobs.service._persist_blob_content -> service._persist_fenced_blob_record:
auto_mutation=auto_mutation,
# tools.blobs._execute_update_blob -> driver.replace_blob:
auto_mutation=context.mutation_ceiling,
# tools.blobs._execute_delete_blob -> service._delete_blob_with_ledger:
auto_mutation=context.mutation_ceiling,
```

Replace the creation business callbacks with these full calls inside their
existing custody `try` (all reconciliation stays unchanged):

```python
self._session_operation_authority.mutate(
    session_operation_context,
    lambda transaction: transaction.blobs.reserve_blob(
        record=reservation, max_storage_per_session=self._max_storage_per_session,
        idempotent=idempotent,
    ), auto_mutation=auto_mutation,
)
```

```python
return self._session_operation_authority.mutate(
    session_operation_context,
    lambda transaction: transaction.blobs.mark_blob_ready(blob_id=blob_id),
    auto_mutation=auto_mutation,
)
```

In replacement, the exact guarded calls are:

```python
plan = self._authority.mutate(
    context,
    lambda transaction: transaction.blobs.prepare_blob_replacement(
        replacement_id=replacement_id, expected=expected, replacement=replacement,
        staging_path=str(staging), backup_path=str(backup),
        max_storage_per_session=max_storage_per_session,
        accepting_proposal_id=accepting_proposal_id,
    ), auto_mutation=auto_mutation,
)
plan = self._authority.mutate(
    context, lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan),
    auto_mutation=auto_mutation,
)
plan = self._authority.mutate(
    context,
    lambda transaction: transaction.blobs.commit_blob_replacement(
        plan=plan, max_storage_per_session=max_storage_per_session,
        accepting_proposal_id=accepting_proposal_id,
    ), auto_mutation=auto_mutation,
)
```

Replace the corresponding two deletion callbacks and its commit transition
with these full calls at their original positions:

```python
plan = self._session_operation_authority.mutate(
    context,
    lambda transaction: transaction.blobs.prepare_blob_deletion(
        blob_id=blob_id, tombstone_path=str(tombstone),
        blob_snapshot_hash=blob_record_snapshot_hash(record),
        expected_file_present=expected_file_present, expected_file_size=expected_file_size,
        expected_file_hash=expected_file_hash, accepting_proposal_id=accepting_proposal_id,
    ), auto_mutation=auto_mutation,
)
```

```python
return self._session_operation_authority.mutate(
    context,
    lambda transaction: transaction.blobs.commit_blob_deletion(
        plan=plan, accepting_proposal_id=accepting_proposal_id,
    ), auto_mutation=auto_mutation,
)
```

```python
plan = self._commit_blob_deletion_plan(
    context=context, plan=plan, accepting_proposal_id=accepting_proposal_id,
    auto_mutation=auto_mutation,
)
```

Existing authenticated API/proposal calls use the default `None`; immediate
`create_blob` likewise retains `None`. The new keyword never applies to
reconciliation, observation or cleanup callbacks.

## A3 — P4 late refusal is an audited result, not a lost cohort

**Modify:** `sessions/_persist_payload.py`, `sessions/service.py`,
`composer/turn_audit.py`, `composer/service.py`, `_compose_loop_carriers.py`,
`composer/visible_tool_event.py`, `composer/protocol.py`.

The existing proposal-specific `TrustModeAutoCommitRevokedError` is unsuitable:
its documented contract says a real proposal remains pending. Use the new
nominal revocation above, plus the minimal public error in A4. No proposal or
proposal event is created by this branch.

- [ ] Add to `AuditOutcome` and `_PersistOutcome`:
  `authority_revocation: ComposerMutationRevocation | None = None`.
  Add to `AuditOutcome`:
  `published_tool_rows: tuple[RedactedToolRow, ...] = ()` and
  `published_assistant_content: str | None = None`.
  These describe rows this transaction actually wrote; fill them in both
  allowed and refused success outcomes, leave them empty on rollback. Extend
  post-init validation to prohibit refusal plus a newly committed checkpoint.
  Do not substitute `no_new_checkpoint` for `empty` on an initially empty graph.

- [ ] Add `mutation_ceiling: ComposerMutationCeiling | None = None` and
  `authority_revocation: ComposerMutationRevocation | None = None` to both
  `persist_compose_turn` and `persist_compose_turn_async`, and forward both in
  the async `_sync` callback. Extend the protocol declaration if exposed there.
  Pass both from the service P4 adapter through module `persist_turn_audit`:
  request ceiling and `dispatch_result.authority_revocation`, respectively.
  Thread the latter through both branches at `turn_audit.py:495,511`, including
  the `required_work=checkpoint_sql` branch. Never create another ticket.

- [ ] Before reserving sequence numbers, inside the existing session lock,
  after the graph-head CAS, decide the complete batch's graph publication:

```python
graph_rows = tuple(row for row in redacted_tool_rows if row.composition_state_payload is not None)
refused = authority_revocation
if graph_rows:
    if mutation_ceiling is None:
        raise AuditIntegrityError("Auto graph checkpoint lost its request ceiling")
    locked_refusal = check_auto_mutation_on_connection(
        conn, context=session_operation_context, ceiling=mutation_ceiling
    )
    if refused is None:
        refused = locked_refusal
```

  A prior actual P3 refusal is not erased by a later upgrade. Validate any
  supplied revocation's ceiling against this session and logical request.
  If `refused` exists, none of this batch's graph candidates may be inserted;
  they form one not-yet-persisted dependent chain. Prior *batches* remain saved.
  Existing CAS, original failure and audit-integrity precedence remain intact.

- [ ] Prepare truthful alternative rows at the P4 owner before SQL submission,
  using each original `ToolResult` and actual base state, then select them under
  the locked predicate. Add `revoked_tool_rows: tuple[RedactedToolRow, ...]`
  to sync/async P4 signatures and forward it through both callers. For every
  graph-payload row, create a replacement with `composition_state_payload=None`
  and content serialized from `_failure_result` with error code
  `auto_mutation_revoked`. The existing public `ToolResult` schema carries
  `success=false`, unchanged base `version`, and the registered code in
  `validation.errors[].error_code`; the tool row has no composition-state ID.
  Do not add public keys under `data`: `_SafeResponseEnvelope` deliberately
  summarizes that open payload. The fixed explanatory detail is in A4's HTTP
  error; private invocation evidence retains the actual preceding blob effect.

  Use the existing redaction walker, not raw `json.dumps` of private arguments.
  Non-graph tool rows remain
  byte-identical, including actual successful blob effects and a real P3 blob
  refusal. For graph-plus-inline results, the original selected result and
  private invocation retain the exact committed inline blob identity; the
  public refusal makes no all-effects-zero assertion.

  Add the following complete pure helper in `turn_audit.py`, reusing its
  existing serializer, `_ToolOutcome`, `RedactedToolRow` and telemetry types.
  `base_state` is an explicit new argument to module `persist_turn_audit` and
  service `_persist_turn_audit`, passed from the captured pre-batch state.
  Import `replace` and `_failure_result` from their existing owners. This
  helper runs after ordinary rows are prepared, before reserving SQL tickets.

```python
def _revoked_graph_rows(
    *, rows: tuple[RedactedToolRow, ...], outcomes: tuple[_ToolOutcome, ...],
    base_state: CompositionState, redaction_telemetry: RedactionTelemetry,
) -> tuple[RedactedToolRow, ...]:
    if tuple(row.tool_call_id for row in rows) != tuple(outcome.call.id for outcome in outcomes):
        raise AuditIntegrityError("Refusal preparation lost its actual ordered tool cohort")
    result: list[RedactedToolRow] = []
    for row, outcome in zip(rows, outcomes, strict=True):
        if row.composition_state_payload is None:
            result.append(row)
            continue
        if row.visible_event is None:
            raise AuditIntegrityError("Graph refusal lost its original visible tool event")
        failed = _failure_result(
            base_state,
            "This candidate graph was not saved because automatic mutation authority was revoked.",
            error_code="auto_mutation_revoked", with_state_validation=False,
        )
        display = _serialize_response_via_walker(
            replace(outcome, response=failed, post_version=outcome.pre_version),
            telemetry=redaction_telemetry, failure_status=None,
        )
        result.append(replace(
            row, content=display, composition_state_payload=None,
            visible_event=row.visible_event.with_display_result(display),
        ))
    return tuple(result)
```

  Import the actual telemetry type already annotating `persist_turn_audit`
  (`RedactionTelemetry`); retain its exact existing annotation if the module
  names it through an alias. New `base_state` is mandatory even when the
  batch has no graph rows, preventing a denied candidate becoming the base.

  Register the new closed error code, preserving the existing response
  redaction model. This is the only public redaction vocabulary extension:

```diff
*** Update File: src/elspeth/web/composer/error_codes.py
@@ _EMITTED_VALIDATION_ERROR_CODES: Final[frozenset[str]] = frozenset(
         "argument_error",
+        "auto_mutation_revoked",
```

  Append these complete tests to the new authority unit module. The first
  exercises the actual walker (including a negative unknown-code control).
  The second is an explicitly storage-layer synthetic `ToolResult` positive
  for the supported sessionless P4 shape; it claims no provider authorship.

```python
import json
from dataclasses import replace

from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage, _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome,
)
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolResult, _failure_result
from elspeth.web.composer.turn_audit import persist_turn_audit

def test_revocation_uses_existing_closed_redaction_schema():
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    failure = _failure_result(state, "PRIVATE diagnostic", error_code="auto_mutation_revoked", with_state_validation=False)
    credential_marker = "sk" + "-proj-" + "x" * 48
    failure = replace(failure, data={
        "candidate": "PRIVATE candidate marker",
        "inline_blob": "PRIVATE inline marker",
        "credential": credential_marker,
    })
    public = redact_tool_call_response("set_pipeline", failure.to_dict(), telemetry=NoopRedactionTelemetry())
    assert public["success"] is False and public["version"] == state.version
    assert [entry["error_code"] for entry in public["validation"]["errors"]] == ["auto_mutation_revoked"]
    assert "PRIVATE" not in json.dumps(public)
    assert credential_marker not in json.dumps(public)
    unknown = failure.to_dict()
    unknown["validation"]["errors"][0]["error_code"] = "PRIVATE-unregistered-code"
    negative = redact_tool_call_response("set_pipeline", unknown, telemetry=NoopRedactionTelemetry())
    assert "PRIVATE" not in json.dumps(negative)

@pytest.mark.asyncio
async def test_sessionless_p4_keeps_graph_result_without_session_event(tmp_path):
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

    base = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    candidate = replace(base, metadata=PipelineMetadata(name="Storage-only sessionless control"), version=2)
    arguments = {"pipeline": _pipeline(tmp_path, "storage-only")}
    call = _AdmittedToolCall("sessionless-control", _AdmittedToolFunction("set_pipeline", json.dumps(arguments)))
    result = ToolResult(success=True, updated_state=candidate, validation=candidate.validate(), affected_nodes=())
    outcome = _ToolOutcome(call=call, response=result, error_class=None, error_category=None,
                           error_message=None, pre_version=1, post_version=2)
    persisted = await persist_turn_audit(
        sessions_service=None, redaction_telemetry=NoopRedactionTelemetry(),
        tool_outcomes=(outcome,), mutation_ceiling=None, authority_revocation=None, base_state=base,
        decoded_args_by_call_id={call.id: arguments}, private_authorings=(), private_invocations=(),
        assistant_message=_AdmittedAssistantMessage(None), raw_assistant_content=None,
        assistant_tool_calls=(call,), crash_pending=False, session_id=None,
        session_operation_context=None, current_state_id=None, persisted_tool_call_turn=False,
        persisted_assistant_message_id=None, persisted_assistant_content=None,
        assistant_row_uses_current_dispatch=True,
    )
    assert persisted.authority_revocation is None
    assert persisted.audit_outcome is None and not persisted.persisted_tool_call_turn
    assert len(persisted.redacted_tool_rows) == 1
    row = persisted.redacted_tool_rows[0]
    assert row.visible_event is None and row.composition_state_payload is not None
    assert json.loads(row.content)["success"] is True
```

  Add this method to `VisibleToolEvent`, retaining its existing schema:

```python
def with_display_result(self, display_result: str) -> VisibleToolEvent:
    if self.parent_assistant_id is not None:
        raise AuditIntegrityError("Only an unpersisted visible event may select its display")
    if type(display_result) is not str:
        raise TypeError("Visible display must be serialized text")
    return replace(self, display_result_hash=_hash(display_result))
```

  The selected result inside the immutable payload remains the actual P3
  candidate result; the display explicitly reports P4 rejection. Its new hash
  binds that actual display. Do not rewrite the private invocation status as
  though P3 itself had failed. Validate alternative rows before SQL: same
  ordered IDs, same event/argument/private identities, no graph payloads,
  unchanged non-graph rows, and a refusal display for each graph row. The SQL
  owner selects the entire allowed or refused tuple; caller booleans cannot
  select the allowed tuple without the locked check.

  Select the rows and assistant bytes only after the locked check, before the
  existing `_reserve_sequence_range`; then the existing insertion loop uses
  these selected locals. Keep the original private evidence tuple unchanged.

```python
published_tool_rows = revoked_tool_rows if refused is not None else redacted_tool_rows
published_assistant_content = "" if refused is not None else assistant_content
redacted_tool_rows = published_tool_rows
assistant_content = published_assistant_content
```

  Every normal `AuditOutcome` return includes these selected fields and
  `authority_revocation=refused`. Its witness construction still selects only
  actual inserted graph IDs, and its current-state ID remains the actual
  locked head. Validate the alternate tuple before entering SQL as follows:

```python
if len(revoked_tool_rows) != len(redacted_tool_rows):
    raise AuditIntegrityError("Refusal alternate changed the real tool cohort")
for original, alternative in zip(redacted_tool_rows, revoked_tool_rows, strict=True):
    if original.composition_state_payload is None:
        if alternative != original:
            raise AuditIntegrityError("Refusal alternate changed an independent tool result")
    elif (alternative.tool_call_id != original.tool_call_id
            or alternative.composition_state_payload is not None
            or ((original.visible_event is None) != (alternative.visible_event is None))
            or (original.visible_event is not None
                and alternative.visible_event != original.visible_event.with_display_result(alternative.content))):
        raise AuditIntegrityError("Refusal alternate changed candidate evidence or retained graph authority")
```

- [ ] Under refusal, set the P4 assistant display content to the empty string
  while preserving original `raw_content`, actual tool calls and private
  authoring evidence. The actual tool refusal rows and A4 error explain the
  outcome. Do not publish the provider's candidate prose as a completion
  claim. This introduces no synthetic assistant reply or suffix template;
  the existing empty-content tool-call assistant shape remains valid.

- [ ] Keep assistant + every real executed tool row + every private evidence
  row in the same existing transaction. Do not add a rejection record that
  claims the planner saw this refusal: `RejectionRecord.planner_payload` means
  the actual feedback sent to a planner, and no further model call is required
  here. The selected refusal display and `AuditOutcome.authority_revocation`
  represent the actual commit decision. No `proposal.rejected` event or fake ID.
  Return actual content/rows and refusal only after transaction commit.

- [ ] In `turn_audit`, read `published_tool_rows` and
  `published_assistant_content` from the actual `AuditOutcome`, replacing the
  precomputed display fields in `_PersistOutcome`. Keep the exact real
  checkpoint witness flow when allowed. Complete the existing projection
  ticket after projecting the actual outcome, including refusal; failure to
  write audit still raises the original integrity failure. Refusal is not a
  reason to abandon a ticket or skip original provider sidecars.

- [ ] In `_compose_loop`, before assigning `state = dispatch.state`, select:

```python
if persist.authority_revocation is None:
    state = dispatch.state
else:
    state = pre_batch_state
```

  Set `pre_batch_state = state` immediately before defining
  `_dispatch_and_persist_tool_turn`; its default-bound `_state` remains the same
  object. Never reconstruct it from
  the denied candidate. Set mutation-success/Advisor-repair/elision updates
  only when the graph batch actually committed. Record actual P4 metadata
  first, then apply the existing plugin/cancellation/checkpoint failure
  precedence. If no higher-priority failure exists, raise the A4 error with
  the actual refusal, recorded LLM calls, committed metadata and original base.
  No P5, END, new primary or tool replay follows late refusal.

### A3 concrete P4 and loop edits

These are contextual signature/call/return edits to the existing owners; they
do not replace their required-work joining, exception precedence or SQL bodies.
The `revoked_tool_rows` argument is required, including for nongraph batches
(whose alternative is exactly the original tuple). Direct persistence tests
pass the real alternative prepared from their supplied storage cohort.

```diff
*** Update File: src/elspeth/web/sessions/service.py
@@ def persist_compose_turn(
         redacted_tool_rows: tuple[RedactedToolRow, ...],
+        revoked_tool_rows: tuple[RedactedToolRow, ...],
+        mutation_ceiling: ComposerMutationCeiling | None = None,
+        authority_revocation: ComposerMutationRevocation | None = None,
@@ def persist_compose_turn_async(
         redacted_tool_rows: tuple[RedactedToolRow, ...],
+        revoked_tool_rows: tuple[RedactedToolRow, ...],
+        mutation_ceiling: ComposerMutationCeiling | None = None,
+        authority_revocation: ComposerMutationRevocation | None = None,
@@ def _sync() -> AuditOutcome:
                 redacted_tool_rows=redacted_tool_rows,
+                revoked_tool_rows=revoked_tool_rows,
+                mutation_ceiling=mutation_ceiling,
+                authority_revocation=authority_revocation,
@@
                     checkpoint=checkpoint,
+                    authority_revocation=refused,
+                    published_tool_rows=published_tool_rows,
+                    published_assistant_content=published_assistant_content,
*** Update File: src/elspeth/web/sessions/_persist_payload.py
@@ class AuditOutcome:
     current_state_id: str | None = None
+    authority_revocation: ComposerMutationRevocation | None = None
+    published_tool_rows: tuple[RedactedToolRow, ...] = ()
+    published_assistant_content: str | None = None
```

Insert these checks at the start of `AuditOutcome.__post_init__`, before its
existing checkpoint-disposition validation:

```python
if self.authority_revocation is not None:
    if type(self.authority_revocation) is not ComposerMutationRevocation:
        raise TypeError("P4 refusal requires an owned revocation")
    if self.checkpoint_disposition == "committed" or self.checkpoint is not None:
        raise ValueError("Refused graph cannot have a new committed checkpoint")
if self.assistant_id is None:
    if self.published_tool_rows or self.published_assistant_content is not None:
        raise ValueError("Rolled-back P4 cannot expose published rows")
```

Keep existing direct `AuditOutcome` constructor compatibility; require the new
publication fields at the actual module P4 boundary, not when historical tests
construct a nominal old outcome solely to test its checkpoint rules. Insert
after the actual `audit_outcome` type check, before projection:

```python
if audit_outcome.assistant_id is not None and audit_outcome.published_assistant_content is None:
    raise AuditIntegrityError("Committed P4 omitted its actual publication content")
```

Insert the following after `redacted_tool_rows = tuple(redacted_rows)` in
`turn_audit.persist_turn_audit`:

```python
revoked_tool_rows = (
    _revoked_graph_rows(
        rows=redacted_tool_rows, outcomes=tool_outcomes, base_state=base_state,
        redaction_telemetry=redaction_telemetry,
    )
    if session_id is not None else redacted_tool_rows
)
```

Both `sessions_service.persist_compose_turn_async` calls receive exactly:

```python
revoked_tool_rows=revoked_tool_rows,
mutation_ceiling=mutation_ceiling,
authority_revocation=authority_revocation,
```

After `unwind_audit_failed = audit_outcome.unwind_audit_failed`, and only after
the actual SQL outcome arrives, insert:

```python
authority_revocation = audit_outcome.authority_revocation
redacted_tool_rows = audit_outcome.published_tool_rows
```

Replace its inferred assistant-content assignment and extend the final
`_PersistOutcome` constructor:

```diff
@@
-        persisted_assistant_content = None if audit_outcome.assistant_id is None else (assistant_message.content or "")
+        persisted_assistant_content = audit_outcome.published_assistant_content
@@
             effect_verification_failure=effect_verification_failure,
+            authority_revocation=authority_revocation,
```

At sync P4 entry, before the alternate-row validation, validate supplied
revocation against the exact request object:

```python
if authority_revocation is not None:
    if (type(authority_revocation) is not ComposerMutationRevocation
            or mutation_ceiling is None
            or authority_revocation.ceiling != mutation_ceiling):
        raise AuditIntegrityError("P4 refusal differs from its logical request ceiling")
    authority_revocation.ceiling.validate_context(session_operation_context)
```

In `tool_batch.run_tool_batch`, after the existing per-outcome classification
and before `if admitted_result is not None and preview_source_proof_stop(admitted_result, state) is not None:`, insert:

```python
if acc.authority_revocation is not None:
    break
```

Its `_DispatchOutcome(...)` constructor receives:

```python
authority_revocation=acc.authority_revocation,
```

In the loop's EARLY condition append
`and dispatch_result.authority_revocation is None`. Replace its state/repair
update block with the following, retaining all subsequent actual P4 metadata
assignments and existing plugin/cancellation/checkpoint precedence:

```python
state = dispatch.state if persist.authority_revocation is None else pre_batch_state
if persist.authority_revocation is None:
    last_validation = dispatch.last_validation
    last_runtime_preflight = dispatch.last_runtime_preflight
    if dispatch.mutation_success_observed:
        mutation_success_seen = True
    advisor_review_state = _record_advisor_repair_mutations(
        advisor_review_state, dispatch.tool_outcomes,
    )
```

The elision condition becomes:

```python
if (pending_advisor_elision_indices and dispatch.mutation_success_observed
        and persist.authority_revocation is None):
```

After the existing `persist.effect_verification_failure` raise and immediately
before `classify = await self._classify_and_budget_turn`, insert:

```python
if persist.authority_revocation is not None:
    raise ComposerMutationAuthorityRevoked(
        revocation=persist.authority_revocation,
        llm_calls=recorder.llm_calls, failed_turn=failed_turn,
        state=state, initial_version=initial_version,
    )
```

### A3 existing direct persistence test adapters

Update the explicit `SessionServiceProtocol.persist_compose_turn_async`
declaration at `sessions/protocol.py:3571` with the same new keywords as its
implementation. Its existing row annotation is `tuple[Any, ...]`; preserve it
for this bounded change:

```diff
*** Update File: src/elspeth/web/sessions/protocol.py
@@ async def persist_compose_turn_async(
         redacted_tool_rows: tuple[Any, ...],
+        revoked_tool_rows: tuple[Any, ...],
+        mutation_ceiling: ComposerMutationCeiling | None = None,
+        authority_revocation: ComposerMutationRevocation | None = None,
```

Add this complete helper to
`tests/unit/web/sessions/session_test_authority.py`, next to its existing
`private_evidence_for_test_turn`. This is explicitly synthetic storage-fixture
preparation, not a production request ceiling. It reads the actual seeded
preference and actual predecessor row; it never assumes auto, changes a
preference, creates a proposal, or overwrites an explicit test ceiling. The
authority regression tests always supply their original ceiling explicitly.

```python
def mutation_inputs_for_test_turn(service, kwargs):
    from dataclasses import replace

    from sqlalchemy import select

    from elspeth.contracts.composer_mutation import ComposerMutationCeiling
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.redaction import redact_tool_call_response
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools._common import _failure_result
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.models import composition_states_table, sessions_table

    result = dict(kwargs)
    if "mutation_ceiling" in result and "revoked_tool_rows" in result:
        return result
    sid = str(result["session_id"])
    context = result["session_operation_context"]
    with service._engine.connect() as connection:
        mode = connection.execute(select(sessions_table.c.trust_mode)
                                  .where(sessions_table.c.id == sid)).scalar_one()
        parent = result["expected_current_state_id"]
        previous = (connection.execute(select(composition_states_table).where(
            composition_states_table.c.id == parent,
            composition_states_table.c.session_id == sid,
        )).one_or_none() if parent is not None else None)
    if "mutation_ceiling" not in result:
        result["mutation_ceiling"] = ComposerMutationCeiling(
            sid, "storage-fixture:" + context.fence.operation_id, mode,
        )
    if "revoked_tool_rows" not in result:
        base = (state_from_record(service._row_to_state_record(previous)) if previous is not None else
                CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1))
        alternative = []
        for row in result["redacted_tool_rows"]:
            if row.composition_state_payload is None:
                alternative.append(row)
                continue
            failure = _failure_result(base, "Automatic mutation authority was revoked.",
                                      error_code="auto_mutation_revoked", with_state_validation=False)
            # These legacy SQL fixtures include deliberately unregistered
            # names such as "f". Only their alternative display uses the
            # registered common graph-result schema; actual call identity and
            # original selected payload remain unchanged.
            content = canonical_json(redact_tool_call_response(
                "set_pipeline", failure.to_dict(), telemetry=NoopRedactionTelemetry()))
            event = row.visible_event.with_display_result(content) if row.visible_event is not None else None
            alternative.append(replace(row, content=content, composition_state_payload=None, visible_event=event))
        result["revoked_tool_rows"] = tuple(alternative)
    return result
```

The exact existing harness method replacements are:

```python
def persist_compose_turn(self, **kwargs):
    kwargs = mutation_inputs_for_test_turn(self, kwargs)
    if "private_tool_evidence" not in kwargs:
        kwargs["private_tool_evidence"] = private_evidence_for_test_turn(kwargs)
    return super().persist_compose_turn(**kwargs)

async def persist_compose_turn_async(self, **kwargs):
    kwargs = mutation_inputs_for_test_turn(self, kwargs)
    if "private_tool_evidence" not in kwargs:
        kwargs["private_tool_evidence"] = private_evidence_for_test_turn(kwargs)
    return await super().persist_compose_turn_async(**kwargs)
```

`tests/helpers/session_fences.py::FencedComposeTurnHarness.persist_compose_turn`
already acquires the actual fixture fence and delegates to this harness; leave
its body intact. The source census maps the existing direct calls as follows;
these are exact fixture owners, not a request to replace concrete services:

| Existing callers | Actual adapter owner |
| --- | --- |
| `test_persist_compose_turn.py`, `test_battery_primary_api.py`, `test_composition_rejection_events.py`, `test_audit_failure_primacy.py` | `FencedSessionServiceHarness`, with the existing exact context wrapper in the first module |
| `test_private_evidence_persistence.py`, `test_visible_tool_event_persistence.py` | `_quota_service` from `test_token_usage_adapters.py` returns `FencedSessionServiceHarness` |
| `test_routes.py`, `test_messages_route_rejection_reasons.py` | Existing application fixture uses the fenced harness |
| `test_compose_loop_concurrent_sessions.py`, `test_compose_loop_latency_sanity.py`, `test_inv_audit_ahead_backward.py` | Existing fenced fixture inheritance |
| `test_composer_splice_concurrency.py` | Existing PostgreSQL `FencedSessionServiceHarness` fixture |
| `test_composer_operation_authority.py::test_required_intermediate_cancellation_joins_actual_sql` | Concrete `SessionServiceImpl`, exact edit below |

The sole concrete direct call in that census has **no tool rows** and no
business mutation. Preserve the concrete class and add its actual empty
alternative, without minting an authority ceiling:

```diff
*** Update File: tests/unit/web/coordination/test_composer_operation_authority.py
@@ async def test_required_intermediate_cancellation_joins_actual_sql(operation_store, monkeypatch, writer):
             redacted_tool_rows=(),
+            revoked_tool_rows=(),
             private_tool_evidence=(),
```

Existing payload-only `AuditOutcome` constructor tests need no new fields.
The new A5 worker tests deliberately keep the concrete implementation and
explicit original ceiling. The persisted fixture default is actually
`auto_commit` (`sessions/models.py:236`); the adapter reads it and never
changes it based on a graph payload. Add this negative/positive adapter test
to `test_persist_compose_turn.py`, whose existing `service` fixture and
`_make_session` seed the actual fence:

```python
@pytest.mark.parametrize("mode", ["explicit_approve", "auto_commit"])
def test_storage_adapter_uses_actual_preference_without_manufacturing_auto(service, mode):
    from sqlalchemy import select, update
    from elspeth.web.sessions.models import sessions_table
    from tests.unit.web.sessions.session_test_authority import mutation_inputs_for_test_turn

    sid = "storage-authority-preference"
    with service._engine.begin() as connection:
        _make_session(connection, session_id=sid)
        connection.execute(update(sessions_table).where(sessions_table.c.id == sid).values(trust_mode=mode))
    values = dict(session_id=sid, session_operation_context=_test_compose_context(sid),
                  expected_current_state_id=None, redacted_tool_rows=(), redacted_assistant_tool_calls=())
    prepared = mutation_inputs_for_test_turn(service, values)
    assert prepared["mutation_ceiling"].initial_trust_mode == mode
    assert prepared["mutation_ceiling"].permits_auto("auto_commit") is (mode == "auto_commit")
    assert prepared["revoked_tool_rows"] == ()
    with service._engine.connect() as connection:
        assert connection.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == sid)).scalar_one() == mode
```

## A4 — Public error, progress and detached outcome

**Modify:** `composer/protocol.py`, `sessions/composer_turn.py`,
`sessions/composer_operation_errors.py`, `sessions/composer_operations.py`,
`contracts/composer_progress.py`, frontend `src/types/composerOperations.ts`,
`src/types/index.ts`, `src/api/composerOperationDecoder.ts`,
`src/hooks/useComposer.ts` (verification only; it delegates to the session
store and needs no new authoring route; all frontend paths under
`src/elspeth/web/frontend/`).

- [ ] Add `ComposerMutationAuthorityRevoked(ComposerServiceError)` carrying
  keyword-only `revocation`, `llm_calls`, `failed_turn`, `state` and
  `initial_version`; require exact nominal revocation. It exposes `http_status
  = 409` and `to_payload()` returning only:

```python
{
    "error_type": "composer_mutation_authority_revoked",
    "detail": "Automatic mutation authority was revoked. Review the saved session and tool evidence before sending another request.",
    "request_id": self.revocation.ceiling.request_id,
    "initial_trust_mode": self.revocation.ceiling.initial_trust_mode,
    "current_trust_mode": self.revocation.current_trust_mode,
}
```

  Do not include raw arguments, an uncommitted candidate state, proposal IDs or
  invented saved revision IDs. Actual saved-revision counts remain the existing
  operation SQL projection's responsibility. Keep the exact original failure
  object when grouping it with audit/worker/cancellation failures.

  Full owned constructor and serializer in `composer/protocol.py`:

```python
class ComposerMutationAuthorityRevoked(ComposerServiceError):
    http_status = 409

    def __init__(
        self, *, revocation: ComposerMutationRevocation,
        llm_calls: tuple[ComposerLLMCall, ...], failed_turn: FailedTurnMetadata | None,
        state: CompositionState, initial_version: int,
    ) -> None:
        if type(revocation) is not ComposerMutationRevocation:
            raise TypeError("Mutation failure requires its actual owned revocation")
        self.revocation = revocation
        self.llm_calls = llm_calls
        self.failed_turn = failed_turn
        self.partial_state = state if state.version > initial_version else None
        self.tool_invocations = ()
        super().__init__(
            "Automatic mutation authority was revoked. Review the saved session and tool evidence before sending another request."
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "error_type": "composer_mutation_authority_revoked",
            "detail": str(self.args[0]),
            "request_id": self.revocation.ceiling.request_id,
            "initial_trust_mode": self.revocation.ceiling.initial_trust_mode,
            "current_trust_mode": self.revocation.current_trust_mode,
        }
```

  Import `ComposerMutationRevocation` from the new contracts owner; existing
  `ComposerLLMCall`, `FailedTurnMetadata` and `CompositionState` imports remain.
  The actual saved-revision projection, rather than `partial_state`, determines
  public revision IDs/counts. An inherited nonempty base is not a new save.

- [ ] Add a dedicated `except ComposerMutationAuthorityRevoked` before generic
  `ComposerServiceError` in `sessions/composer_turn.py`. Set
  `observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)`;
  publish failed progress with reason `mutation_authority_revoked`, the fixed
  safe detail, and recovery text `Review the saved session before retrying.`
  Preserve the existing required-audit finalizer, deduplication and settlement
  observation; then raise `HTTPException(status_code=409, detail=exc.to_payload())
  from exc`. Do not re-persist P4 tool rows. The finalizer owes the actual
  primary/EARLY provider audit regardless of refusal.

- [ ] Add the nominal error to `project_composer_operation_error` after the
  integrity/storage precedence branches and before generic failure:
  return `_http_error(409, {"detail": leaf.to_payload()},
  "composer_mutation_authority_revoked")`. Its `failure_code` remains existing
  `http_error`. Add `composer_mutation_authority_revoked` to both Python public
  failure type and `COMPOSER_OPERATION_PUBLIC_FAILURE_TYPES`, and the TypeScript
  union/decoder category array. Add `mutation_authority_revoked` to the Python
  progress reason, frontend progress union and decoder reason array.

- [ ] Add codec/HTTP parity tests in
  `tests/unit/web/sessions/test_composer_operation_error_parity.py`,
  `test_composer_operation_codec.py`, frontend
  `src/api/composerOperationDecoder.test.ts` and `src/hooks/useComposer.test.ts`.
  Decode a real projected refusal, show safe reason and reload actual session
  state, preserve previously saved revisions, and reject unknown categories
  and spoofed proposal fields. No automatic retry or synthetic proposal UI.
  Historical error categories continue to decode unchanged.

### A4 route, projector and wire edits

Import `ComposerMutationAuthorityRevoked` from `composer.protocol` in the two
Python consumers. Insert this entire handler before the existing generic
`except ComposerServiceError`; the enclosing required-audit finalizer already
drains `observation.pending_exception_llm_calls` exactly once:

First add the service-level passthrough so it does not mislabel this failure
as prompt setup before the route publishes the actual reason:

```diff
*** Update File: src/elspeth/web/composer/service.py
@@
             except PromptGuardRefused:
                 # Each invocation owner captured its exact undrained evidence.
                 # A completed planner drain must remain empty here.
                 raise
+            except ComposerMutationAuthorityRevoked:
+                raise
             except ComposerServiceError:
```

```python
except ComposerMutationAuthorityRevoked as exc:
    observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
    await _publish_progress(
        progress_sink,
        event=ComposerProgressEvent(
            phase="failed",
            headline="Automatic mutation authority was revoked.",
            evidence=(str(exc),),
            likely_next="Review the saved session before retrying.",
            reason="mutation_authority_revoked",
        ),
    )
    raise HTTPException(status_code=409, detail=exc.to_payload()) from exc
```

In `project_composer_operation_error`, after `leaf = leaves[0]` and before
`PromptGuardRefused`, insert:

```python
if isinstance(leaf, ComposerMutationAuthorityRevoked):
    mutation_payload: dict[str, JsonValue] = {**leaf.to_payload()}
    return _http_error(409, {"detail": mutation_payload}, "composer_mutation_authority_revoked")
```

The mounted handler's `HTTPException` projection must also retain the category
through the existing category allowlist. These exact vocabulary additions
apply to the existing literals/arrays, preserving every other member:

```diff
*** Update File: src/elspeth/web/sessions/composer_operations.py
@@ type ComposerOperationPublicFailureType = Literal[
     "stale_compose_state",
+    "composer_mutation_authority_revoked",
@@ COMPOSER_OPERATION_PUBLIC_FAILURE_TYPES: Final[frozenset[str]] = frozenset(
         "stale_compose_state",
+        "composer_mutation_authority_revoked",
*** Update File: src/elspeth/contracts/composer_progress.py
@@ type ComposerProgressReason = Literal[
     "service_setup_failed",
+    "mutation_authority_revoked",
*** Update File: src/elspeth/web/frontend/src/types/index.ts
@@ export type ComposerProgressReason =
   | "service_setup_failed"
+  | "mutation_authority_revoked"
```

In `frontend/src/types/composerOperations.ts`, replace the complete existing
`ComposerOperationPublicFailureType` declaration with:

```typescript
export type ComposerOperationPublicFailureType = "request_cancelled" | "composer_operation_deadline_expired" | "composer_operation_worker_lost" | "operation_failed" | "audit_integrity_error" | "database_unavailable" | "storage_unavailable" | "stale_compose_state" | "fingerprint_key_missing" | "secret_decryption_failed" | "composer_mutation_authority_revoked";
```

In `frontend/src/api/composerOperationDecoder.ts`, replace its two complete
closed-vocabulary declarations:

```typescript
const categories: readonly ComposerOperationPublicFailureType[] = ["request_cancelled", "composer_operation_deadline_expired", "composer_operation_worker_lost", "operation_failed", "audit_integrity_error", "database_unavailable", "storage_unavailable", "stale_compose_state", "fingerprint_key_missing", "secret_decryption_failed", "composer_mutation_authority_revoked"];
```

```typescript
const reasons = ["convergence_composition_budget", "convergence_discovery_budget", "convergence_wall_clock_timeout", "tool_call_cap_exceeded", "provider_auth_failed", "provider_unavailable", "plugin_crash", "runtime_preflight_failed", "planner_repair_exhausted", "service_setup_failed", "admission_refused", "accounting_unavailable", "client_cancelled", "composer_idle", "composer_complete", "mutation_authority_revoked"] as const;
```

Append these tests to `test_composer_operation_error_parity.py`. They compare
the real route exception shape and the nominal projector, and prove integrity
precedence rather than treating the new refusal as a generic failure:

```python
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.web.composer.protocol import ComposerMutationAuthorityRevoked
from elspeth.web.composer.state import CompositionState, PipelineMetadata

def mutation_error():
    return ComposerMutationAuthorityRevoked(
        revocation=ComposerMutationRevocation(
            ComposerMutationCeiling("11111111-1111-4111-8111-111111111111", "request", "auto_commit"),
            "explicit_approve",
        ),
        llm_calls=(), failed_turn=None,
        state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        initial_version=1,
    )

def test_mutation_revocation_projects_same_safe_http_outcome():
    error = mutation_error()
    nominal = project_composer_operation_error(error, request_id="request")
    route = project_composer_operation_error(
        HTTPException(status_code=409, detail=error.to_payload()), request_id="request")
    assert nominal.http_status == route.http_status == 409
    assert nominal.failure_code == route.failure_code == "http_error"
    assert nominal.error_type == route.error_type == "composer_mutation_authority_revoked"
    assert nominal.body == route.body == {"detail": error.to_payload()}
    assert error.partial_state is None
    assert "proposal" not in json.dumps(nominal.body)

def test_audit_failure_still_outranks_mutation_revocation():
    error = mutation_error()
    original = AuditIntegrityError("PRIVATE original audit failure")
    projected = project_composer_operation_error(
        ExceptionGroup("original pair", [error, original]), request_id="request")
    assert projected.error_type == "audit_integrity_error"
    assert projected.http_status == 500
    assert "PRIVATE" not in projected.model_dump_json()
```

Append to the existing decoder test module (its `sid` and `id` are already
canonical constants):

```typescript
it("retains actual saved facts for mutation authority refusal without inventing proposals", () => {
  const outcome = {
    session_id: sid, operation_id: id, kind: "compose_message", user_message_id: null,
    settled_at: "2026-10-22T08:00:00Z", failure_code: "http_error",
    error_type: "composer_mutation_authority_revoked", diagnostic_id: null,
    request_id: "request", saved_revisions: { attribution: "fenced", count: 1, latest_state_id: id, latest_version: 2 },
  };
  const page = { session_id: sid, items: [outcome], next_cursor: null };
  expect(decodeComposerOperationFailurePage(page, sid).items[0]).toEqual(outcome);
  for (const changes of [{ proposal_id: id }, { error_type: "invented_auto_proposal" }]) {
    expect(() => decodeComposerOperationFailurePage({ ...page, items: [{ ...outcome, ...changes }] }, sid)).toThrow();
  }
  const frame = { ...heartbeat, event: "progress", payload: {
    session_operation_id: id, session_operation_epoch: 1, request_token: "token", request_id: "request",
    phase: "failed", headline: "Automatic mutation authority was revoked.", evidence: [],
    likely_next: "Review the saved session before retrying.", reason: "mutation_authority_revoked",
    updated_at: "2026-10-22T08:00:00Z",
  } };
  expect(decodeComposerOperationFrame(frame, sid, id)).toEqual(frame);
  expect(() => decodeComposerOperationFrame({ ...frame, payload: { ...frame.payload, reason: "invented_auto_proposal" } }, sid, id)).toThrow();
});
```

Append this hook preservation test to its existing `describe`; no production
hook change is needed because it exposes the actual session store state:

```typescript
it("shows an authority refusal without retrying or adding a proposal", () => {
  const send = vi.fn();
  const retry = vi.fn();
  const detail = "Automatic mutation authority was revoked. Review the saved session before retrying.";
  useSessionStore.setState({
    error: detail, sendMessage: send, retryMessage: retry,
    isComposing: false, compositionProposals: [],
  });
  const { result } = renderHook(() => useComposer());
  expect(result.current.error).toBe(detail);
  expect(result.current.isComposing).toBe(false);
  expect(useSessionStore.getState().compositionProposals).toEqual([]);
  expect(send).not.toHaveBeenCalled();
  expect(retry).not.toHaveBeenCalled();
});
```

## A5 — Real SDK, workers, SQL and red controls

**Create:** `tests/integration/web/composer/test_common_mutation_authority.py`,
`tests/integration/web/composer/test_common_mutation_authority_cancellation.py`,
`tests/unit/web/sessions/test_common_mutation_authority_workers.py`,
`tests/testcontainer/web/test_common_mutation_authority_workers_postgres.py`.
**Retain/adapt narrowly:** `test_freeform_pipeline_planner.py`,
`test_freeform_planner_failure_translation.py` in the same integration folder.

- [ ] Reuse the actual `_build_app`, `_Response`, `_Choice`, `_Message`,
  `_ToolCall`, `_Function` setup from
  `test_freeform_planner_failure_translation.py` using the explicit imports
  in the complete modules below. Keep its `FencedSessionServiceHarness`, real
  `ComposerServiceImpl`, async operation worker and `settle_sync`. Do not use
  the `_CleanAdvisor` fixture in proposal-prevalidation tests for this packet.
  Patch only physical `litellm.acompletion` with a specced `AsyncMock` whose
  side effect validates current declarations and supplies finite responses.

  Primary mutation response construction (a helper in the new test module):

```python
def mutation_response(*, pipeline, ordinal):
    return _Response(
        choices=[_Choice(_Message(None, [_ToolCall(
            id=f"authority-set-pipeline-{ordinal}",
            function=_Function("set_pipeline", json.dumps({"pipeline": pipeline})),
        )]))],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        model="provider/common-authority",
        id=f"authority-response-{ordinal}",
    )
```

  Copy `_pipeline`'s current CSV→JSONL body from
  `test_freeform_pipeline_planner.py:86–113`; create its session-contained CSV
  input using the fixture's real storage writer and create output directory.
  For inline tests use the current `source.inline_blob` schema from
  `test_freeform_proposal_prevalidation.py::_inline_pipeline_args`, retaining
  all ordinary interpretation requirements. No private terminal call.

- [ ] Use an explicit finite physical sequence, not a permissive fallback:

| Scenario | Actual allowed sequence |
| --- | --- |
| Empty auto mutation | primary `set_pipeline`; actual EARLY Advisor; primary prose; actual END publication review. |
| Existing nonempty auto mutation | primary `set_pipeline`; primary prose; actual END. |
| Explicit/review path | primary `set_pipeline`; primary review-pending prose; actual END. No EARLY because no graph transition applied. |
| Late graph refusal at P4 | primary `set_pipeline`; EARLY only if it actually started before P4; then refusal. No new primary or END. |
| Early blob business refusal | primary; actual refused tool/P4; no new EARLY/primary/END. |

  Observe the real `_run_advisor_checkpoint` by wrapping and delegating to the
  original method to record its `phase`; do not return an internal verdict.
  At physical Advisor SDK entry assert the recorded phase and model, actual
  messages and `response_format`. EARLY response uses actual JSON
  `{"verdict":"CLEAN","category":"other","steps":[],"findings":"No structural mismatch in the supplied approach.","note":null}`.
  END response adds `"publication_verdict":"ADMIT"`. Parse its real
  `AdvisorPublicationReview` using `parse_advisor_publication_review`, assert
  exact current request, candidate reply, graph receipts and saved effect
  count, and bind returned admission to that actual call. Each physical
  response has a fresh response ID; each authored tool call has a fresh ID.
  Known cost is included so these authority tests do not trip G3 accidentally.

- [ ] Add a held-provider test using real preference changes. The following
  complete callback is installed as the first primary script action; the test
  starts `settle_sync` in a thread, waits for `entered`, PATCHes preferences
  through the authenticated client, and releases. This holds a real physical
  SDK await, not a mocked compose loop or simulated current read.

```python
entered = threading.Event()
release = threading.Event()

async def held_primary(**kwargs):
    names = {tool["function"]["name"] for tool in kwargs["tools"]}
    assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
    entered.set()
    assert await asyncio.to_thread(release.wait, 15), "held SDK response was not released"
    return mutation_response(pipeline=pipeline, ordinal=1)
```

  Parameterize initial/final modes over auto/auto, explicit/explicit and both
  flips; parameterize absent/nonempty bases and send/recompose. Auto/auto must
  produce a real new graph row and linked tool evidence. The other cases must
  produce a real pending proposal and no new graph. Assert actual proposal
  `tool_call_id`, arguments hash and event rows, not only response wording.
  For a nonempty base assert exact preexisting ID/hash/bytes survive refusal.

- [ ] Add upgrade-between-iterations using primary discovery then primary
  `set_pipeline`, with upgrade committed after the first actual P4 and before
  the second physical primary. The same captured ceiling remains explicit;
  the second mutation creates a pending proposal. No per-iteration recapture.
  Delete only the initial-mode conjunct as a process-local mutant: both upgrade
  tests and the over-cap test must turn RED by effect/proposal assertions.

- [ ] Add a late downgrade wrapper on `sessions.persist_compose_turn_async`
  that pauses **before** delegating to the original. Assert dispatch current
  mode was auto and the real candidate exists; commit the actual preference
  PATCH; release P4. Assert HTTP 409/category, zero new graph rows, actual P4
  assistant/tool/private evidence, exact provider-call audit IDs, no fake
  proposal/event, zero later SDK calls, unchanged prior state. Remove only
  the locked P4 predicate as a mutant: this test must observe an illegal graph
  row and turn RED. A fixture exception before the barrier is not a valid RED.

- [ ] Add the inverse graph lock order by wrapping
  `_insert_composition_checkpoint` and delegating after a thread barrier while
  the real transaction holds its session lock. Start preference PATCH on
  another connection/process, prove it has not completed, then release commit.
  Assert the graph and its witness are actually committed; PATCH completes
  after it; never report revocation for that already committed graph.
  No sleeps determine the order: entered/release/completed barriers do.

- [ ] Add inline creation, update and delete authority tests at actual custody
  owners. Hold before first guarded `mutate`, commit downgrade, then release:
  compare real blobs rows, journal rows and canonical file bytes to baseline;
  no unapproved business effect. Hold after guarded reservation/intent while
  custody is held, start PATCH, prove it waits, release and prove commit wins.
  Add inline-created-before-P4-downgrade: the real ready blob remains recorded,
  graph is refused, candidate is never displayed as applied. Add deliberate
  `create_blob` in explicit mode: actual blob ready row and bytes must commit
  immediately, with zero graph rows and no approval proposal. Mutating the
  exception into a global COMPOSE guard must turn this positive control RED.
  Temporarily omit preference custody locking: PostgreSQL filesystem barrier
  test must show PATCH completing during guarded filesystem work and turn RED.

- [ ] Keep cancellation separate from durable Stop. Use real worker barriers
  in `run_required_sql_in_worker` and underlying `persist_compose_turn`, as the
  existing persistence cancellation tests do. Required cases and exact oracles:

| Case | Required observable result |
| --- | --- |
| Cancel before any tool starts | Original cancellation survives; actual completed primary audit retained; zero tool effects/graph/proposal; no extra SDK. |
| Cancel before required P4 admission, or cancel an ordinary non-required queued P4 | Actual worker never begins; no invented committed checkpoint; existing no-submission disposition observed; no retry. A submitted required Future is joined even while queued. |
| Cancel after P4 worker begins, transaction commits | Caller remains joined until release; actual assistant/tool/private cohort and graph witness survive; original cancellation resumes after metadata adoption. |
| Cancel after worker begins, original SQL failure | Atomic rollback; no partial P4 cohort/new graph; exact original SQL exception remains reachable with original cancellations; no retry. |
| Repeated cancellation while joining started SQL | Both original cancellation objects/messages preserved by the actual owned finish-once/outer worker path; do not accept only matching class names. Required tickets complete once, lock released only afterward. |
| Explicit common proposal creation cancellation | Real common `create_pipeline_composition_proposal` worker joins; a committed proposal remains pending with its actual created event; never synthesize auto rejection. |
| Durable Stop wins session lock | Real job Stop transition commits before P4 guard; no business graph write; actual stopped operation and required audit/rollback outcome. |
| P4 wins session lock before durable Stop | Actual graph and audit commit; later Stop may govern subsequent work; saved-revision projection truthfully reports the committed prefix. |

  Preserve the two low-level facade finish-once lifecycle tests by calling
  still-used `create_pipeline_composition_proposal_finish_once` and
  `reject_pipeline_composition_proposal_finish_once` directly with real
  `PreparedPipelinePlan`, real proposal IDs and `RequiredWorkBinding` tickets
  reserved from the owning coordinator. Retain their existing create/reject
  worker barriers, pending/rejected row/event assertions, original cancellation
  identity and original rejection failure identity. Remove only dependency on
  public `compose` manufacturing an auto proposal. Reuse the existing setup
  that obtains an actual plan from the retained direct planner library; do not
  insert synthetic proposal rows or hand-construct a fake authority token.
  Cancellation tests for public auto behavior instead use actual common P3/P4.

- [ ] PostgreSQL uses the exact `operation_postgres_target`/`operation_store`
  fixture exports below and real concurrent workers. The collector imports
  the exact graph lock orders, inline/replacement/deletion lock orders,
  preference exclusion, and Stop-vs-P4 cases below. The custody test gives
  the blob writer a child process and the preference writer a separately
  owned parent worker/engine, so a process-local lock cannot pass.
  Both SQL backends are required; SQLite alone is not acceptance.

- [ ] Keep the source-bound 24-function H2.3 mapping in
  `.claude/lanes/first-carry-20261021/h2-facade-retirement.md` and
  `envelope-tool-test-repair.md`. The stale private Advisor fixture must be
  migrated to actual EARLY and actual END bad-request SDK responses with the
  same `SafeRequestRejection`/private evidence and no-effect/public-redaction
  oracles. Keep direct-library manifest tests at that library. No wholesale
  module deletion, generic 500 blessing, private palette restoration or
  changed provider-call counts without a physical request trace.

### A5 executable primary/Advisor and held-race tests

Add this code to the new integration module. It uses the existing actual
application/worker harness and SQL rows.
Imports are explicit, and the only provider replacement is the physical SDK.
The two real checkpoints are wrapped solely to observe phase; they execute
their complete normal body. `CLEAN` is actual returned SDK JSON, not an
internally injected verdict. The `p4` barrier is before the actual worker
submission, giving the preference writer an unambiguous first lock order.

```python
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.sessions.models import (
    blobs_table, chat_messages_table, composer_async_operations_table,
    composition_states_table, composition_proposals_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.advisor_review import parse_advisor_publication_review
from tests.helpers.composer_operations import settle_sync
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
from tests.integration.web.composer.test_freeform_planner_failure_translation import (
    _build_app, _Response, _Choice, _Message, _ToolCall, _Function,
)

def mutation_response(*, pipeline, ordinal):
    return _Response(
        choices=[_Choice(_Message(None, [_ToolCall(
            f"authority-set-pipeline-{ordinal}",
            _Function("set_pipeline", json.dumps({"pipeline": pipeline})),
        )]))],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        model="provider/common-authority", id=f"authority-response-{ordinal}",
    )

def run_authority_case(tmp_path, monkeypatch, *, initial, final, barrier,
                       discovery_first=False, inline=False, blob_only=False,
                       batch_count=1, nonempty=False, recompose=False):
    entered, release = threading.Event(), threading.Event()
    requests, phases = [], []
    current_phase = None
    primary_count = 0
    pipeline = None
    request_text = "Build a CSV to JSONL pipeline."
    expected_auto = (initial == "auto_commit"
                     and (final == "auto_commit" or barrier == "p4_locked")) and not blob_only
    candidate_reply = ("The blob was saved." if blob_only else
                       "The draft was saved." if expected_auto else
                       "The proposal is waiting for approval.")
    mutation_ordinal = 2 if discovery_first else 1

    async def complete(**kwargs):
        nonlocal primary_count
        requests.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            primary_count += 1
            assert primary_count <= mutation_ordinal + 1, "Unexpected extra primary call"
            names = {entry["function"]["name"] for entry in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            if discovery_first and primary_count == 1:
                return _Response(
                    choices=[_Choice(_Message(None, [_ToolCall(
                        "authority-discovery-1", _Function("get_plugin_schema", json.dumps({
                            "plugin_type": "source", "name": "csv",
                        })),
                    )]))],
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                    id="authority-discovery-response",
                )
            if primary_count == mutation_ordinal:
                if barrier in {"provider", "second_provider"}:
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 15)
                assert pipeline is not None
                if blob_only:
                    return _Response(
                        choices=[_Choice(_Message(None, [_ToolCall(
                            "authority-create-blob-1", _Function("create_blob", json.dumps({
                                "filename": "immediate.csv", "mime_type": "text/csv",
                                "content": "value\n42\n", "description": "Actual provider-created data",
                            })),
                        )]))],
                        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                        id="authority-create-response",
                    )
                result = mutation_response(pipeline=pipeline, ordinal=mutation_ordinal)
                if batch_count > 1:
                    result.choices[0].message.tool_calls = [
                        _ToolCall(f"authority-set-pipeline-{n}",
                                  _Function("set_pipeline", json.dumps({"pipeline": pipeline})))
                        for n in range(1, batch_count + 1)
                    ]
                return result
            return _Response(
                choices=[_Choice(_Message(candidate_reply))],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                id="authority-primary-2",
            )
        assert kwargs["model"] == composer._settings.composer_advisor_model
        assert current_phase in {"early", "end"}
        assert phases.count(current_phase) == 1, "Unexpected repeated checkpoint"
        assert kwargs.get("tools") is None and "response_format" in kwargs
        payload = {"verdict": "CLEAN", "category": "other", "steps": [],
                   "findings": "The supplied evidence supports the stated draft outcome.", "note": None}
        if current_phase == "end":
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert review["current_request"] == request_text
            assert review["candidate_reply"] == candidate_reply
            assert review["saved_effects"]["saved_revision_count"] == (1 if expected_auto else 0)
            payload["publication_verdict"] = "ADMIT"
        return _Response(
            choices=[_Choice(_Message(json.dumps(payload)))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model=kwargs["model"], id=f"authority-{current_phase}",
        )

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Authority race"}).json()["id"]
    pipeline = _pipeline(tmp_path, sid)
    if inline:
        pipeline["source"] = {
            "plugin": "csv", "on_success": "rows",
            "options": {"schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
            "inline_blob": {"filename": "candidate.csv", "mime_type": "text/csv",
                            "content": "value\n42\n", "description": "Provider inline source"},
        }
    input_path = tmp_path / "blobs" / sid / "input.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
    # Existing graph fixtures are explicitly session_seed input. This write
    # is not attributed to the request under test or to a provider response.
    base_record = None
    if nonempty:
        base_record = asyncio.run(sessions.save_composition_state(
            UUID(sid), CompositionStateData(
                sources={"source": deepcopy(pipeline["source"])}, nodes=[], edges=[],
                outputs=[{**deepcopy(pipeline["outputs"][0]), "name": "rows"}],
                metadata_={"name": "Existing fixture", "description": ""}, is_valid=True,
            ), provenance="session_seed",
        ))
    with engine.connect() as connection:
        baseline_rows = connection.execute(select(composition_states_table)
            .where(composition_states_table.c.session_id == sid)).mappings().all()
    body = {"content": request_text, "operation_id": str(uuid4()),
            "state_id": str(base_record.id) if base_record is not None else None}
    path = f"/api/sessions/{sid}/messages"
    if recompose:
        user = asyncio.run(sessions.add_message(
            UUID(sid), "user", request_text, writer_principal="route_user_message"))
        body = {"operation_id": body["operation_id"], "state_id": body["state_id"],
                "expected_user_message_id": str(user.id)}
        path = f"/api/sessions/{sid}/recompose"

    def change_mode(mode):
        response = client.patch(f"/api/sessions/{sid}/composer/preferences",
                                json={"trust_mode": mode, "density_default": "high"})
        assert response.status_code == 200, response.text
        assert response.json()["trust_mode"] == mode

    change_mode(initial)
    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    async def observe_checkpoint(**kwargs):
        nonlocal current_phase
        current_phase = kwargs["phase"]
        phases.append(current_phase)
        return await original_checkpoint(**kwargs)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    original_p4 = sessions.persist_compose_turn_async
    async def held_p4(**kwargs):
        if barrier == "p4":
            assert any(row.composition_state_payload is not None for row in kwargs["redacted_tool_rows"])
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
        return await original_p4(**kwargs)
    monkeypatch.setattr(sessions, "persist_compose_turn_async", held_p4)
    original_execute = tool_batch_module.execute_tool
    def held_tool(*args, **kwargs):
        if barrier == "tool":
            assert args[0] == ("create_blob" if blob_only else "set_pipeline")
            entered.set()
            assert release.wait(15)
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(tool_batch_module, "execute_tool", held_tool)
    original_insert = sessions._insert_composition_checkpoint
    def held_insert(*args, **kwargs):
        if barrier == "p4_locked":
            entered.set()
            assert release.wait(15)
        return original_insert(*args, **kwargs)
    monkeypatch.setattr(sessions, "_insert_composition_checkpoint", held_insert)
    preference_attempted = threading.Event()
    original_begin = sessions._session_process_locked_begin
    @contextmanager
    def observe_begin(session_id):
        if barrier == "p4_locked" and entered.is_set() and not release.is_set():
            preference_attempted.set()
        with original_begin(session_id) as connection:
            yield connection
    monkeypatch.setattr(sessions, "_session_process_locked_begin", observe_begin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(settle_sync, client, client.app,
                                  path=path, body=body)
        try:
            assert entered.wait(15), "Actual selected owner was never reached"
            if barrier == "p4_locked":
                preference_change = executor.submit(change_mode, final)
                assert preference_attempted.wait(15)
                assert not preference_change.done()
                release.set()
                preference_change.result(timeout=15)
            else:
                change_mode(final)
        finally:
            release.set()
        settled = pending.result(timeout=30)
    with engine.connect() as conn:
        states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
        proposals = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).mappings().all()
        chat = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)
                            .order_by(chat_messages_table.c.sequence_no)).mappings().all()
        operation = conn.execute(select(composer_async_operations_table)
                                 .where(composer_async_operations_table.c.session_id == sid)).mappings().one()
        blobs = conn.execute(select(blobs_table).where(blobs_table.c.session_id == sid)).mappings().all()
    old_ids = {row["id"] for row in baseline_rows}
    assert [row for row in states if row["id"] in old_ids] == baseline_rows
    states = [row for row in states if row["id"] not in old_ids]
    # These are actual SDK invocations: bind each physical provider response to
    # its durable call receipt and independently authenticated request sidecar.
    calls = [row["tool_calls"][0]["call"] for row in chat
             if row["role"] == "audit" and row["tool_calls"]
             and row["tool_calls"][0].get("_kind") == "llm_call_audit"]
    assert len(calls) == sdk.await_count
    assert len({call["call_id"] for call in calls}) == len(calls)
    assert all(call["status"] == "success" for call in calls)
    private = [ComposerDiagnosticEvidence.from_envelope(
        row["tool_calls"][0], expected_session_id=sid,
        expected_operation_id=operation["session_operation_id"])
        for row in chat if row["role"] == "audit" and row["tool_calls"]
        and row["tool_calls"][0].get("_kind") == "composer_diagnostic_evidence"]
    requests_by_id = {item.tool_call_id: item for item in private if item.status == "provider_request"}
    assert set(requests_by_id) == {call["call_id"] for call in calls}
    assert all(item.operation_epoch == operation["session_operation_epoch"] for item in private)
    for call in calls:
        evidence = requests_by_id[call["call_id"]]
        assert evidence.arguments.content is not None
        request = json.loads(evidence.arguments.content)
        assert request["capture_boundary"] and request["messages"]
    if inline or blob_only:
        expected_blobs = int(blob_only or barrier == "p4" or expected_auto)
        assert len(blobs) == expected_blobs
        for blob in blobs:
            assert blob["status"] == "ready"
            assert Path(blob["storage_path"]).read_bytes() == b"value\n42\n"
    return settled, states, proposals, chat, phases, primary_count, sdk.await_count

@pytest.mark.parametrize("initial,final,applied", [
    ("auto_commit", "auto_commit", True),
    ("explicit_approve", "explicit_approve", False),
    ("auto_commit", "explicit_approve", False),
    ("explicit_approve", "auto_commit", False),
])
def test_held_primary_preserves_initial_and_current_authority(tmp_path, monkeypatch, initial, final, applied):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider")
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert len(states) == int(applied)
    assert len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    assert primary == 2
    assert phases == (["early", "end"] if applied else ["end"])
    assert physical == (4 if applied else 3)
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-set-pipeline-1"]
    assert tools[0]["composition_state_id"] == (states[0]["id"] if applied else None)

@pytest.mark.parametrize("nonempty,recompose", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("initial,final,applied", [
    ("auto_commit", "auto_commit", True),
    ("explicit_approve", "explicit_approve", False),
    ("auto_commit", "explicit_approve", False),
    ("explicit_approve", "auto_commit", False),
])
def test_same_authority_for_existing_graph_and_recompose(
    tmp_path, monkeypatch, nonempty, recompose, initial, final, applied,
):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider",
        nonempty=nonempty, recompose=recompose)
    result = settled.result()
    assert result["message"]["role"] == "assistant"
    assert len(states) == int(applied) and len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    early = applied and not nonempty
    assert phases == (["early", "end"] if early else ["end"])
    assert primary == 2 and physical == 2 + len(phases)
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["tool_call_id"] == "authority-set-pipeline-1"
    if applied:
        assert tool["composition_state_id"] == states[0]["id"]
    else:
        assert tool["composition_state_id"] is None

def test_late_downgrade_refuses_graph_but_keeps_actual_p4_cohort(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4")
    status, body = settled.error()
    assert status == 409, body
    assert body["detail"]["error_type"] == "composer_mutation_authority_revoked"
    assert states == [] and proposals == []
    assert primary == 1 and phases == ["early"] and physical == 2
    tools = [row for row in chat if row["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["tool_call_id"] == "authority-set-pipeline-1"
    assert tools[0]["composition_state_id"] is None
    public = json.loads(tools[0]["content"])
    assert public["success"] is False
    assert [item["error_code"] for item in public["validation"]["errors"]] == ["auto_mutation_revoked"]
    assistant = next(row for row in chat if row["id"] == tools[0]["parent_assistant_id"])
    assert assistant["content"] == ""
    assert len([row for row in chat if row["role"] == "audit" and row["tool_calls"]
                and row["tool_calls"][0].get("_kind") == "llm_call_audit"]) == 2

def test_graph_commit_precedes_later_preference_revocation(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve",
        barrier="p4_locked")
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert len(states) == 1 and proposals == []
    assert primary == 2 and physical == 4 and phases == ["early", "end"]
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["composition_state_id"] == states[0]["id"]
    assert json.loads(tool["content"])["success"] is True

def test_upgrade_between_iterations_does_not_reauthorize(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final="auto_commit",
        barrier="second_provider", discovery_first=True)
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert states == [] and len(proposals) == 1 and proposals[0]["status"] == "pending"
    assert phases == ["end"] and primary == 3 and physical == 4
    assert [row["tool_call_id"] for row in chat if row["role"] == "tool"] == [
        "authority-discovery-1", "authority-set-pipeline-2"]

def test_upgraded_request_still_uses_pending_proposal_cap(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final="auto_commit",
        barrier="provider", batch_count=11)
    status, body = settled.error()
    assert status == 422, body
    detail = body["detail"]
    assert detail["reason"] == "tool_call_cap_exceeded"
    assert detail["evidence"]["cap_kind"] == "pending_proposals"
    assert detail["evidence"]["cap"] == 10
    assert states == [] and proposals == []
    assert not [row for row in chat if row["role"] == "tool"]
    assert primary == physical == 1 and phases == []

@pytest.mark.parametrize("barrier,physical,phases", [
    ("tool", 1, []), ("p4", 2, ["early"]),
])
def test_inline_source_respects_custody_and_retains_actual_prefix(tmp_path, monkeypatch, barrier, physical, phases):
    settled, states, proposals, chat, actual_phases, primary, actual_physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve",
        barrier=barrier, inline=True)
    status, body = settled.error()
    assert status == 409, body
    assert states == [] and proposals == []
    assert primary == 1 and actual_physical == physical and actual_phases == phases
    tools = [row for row in chat if row["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["composition_state_id"] is None
    assert json.loads(tools[0]["content"])["success"] is False

def test_create_blob_remains_immediate_after_downgrade(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve",
        barrier="tool", blob_only=True)
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert states == [] and proposals == []
    assert primary == 2 and physical == 3 and phases == ["end"]
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-create-blob-1"]
    assert json.loads(tools[0]["content"])["success"] is True
```

The A5 tables also require the worker/lock, cancellation, Stop and cross-process
cases below. Every selected barrier must fire before its result assertion can
qualify. The helper delegates actual tool execution and both Advisor bodies;
it never writes graph/proposal/audit rows itself.

### A5 actual update/delete custody controls

Append this complete test to the A5 integration module (it uses that module's
existing imports). The first physical SDK call creates its own unreferenced
blob through the intentional immediate exception. The second physical call
uses that actual returned ID. The held boundary is after dispatch's current
preference read and before the real update/delete custody owner.

```python
@pytest.mark.parametrize("tool", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("final_mode", ["auto_commit", "explicit_approve"])
def test_approval_governed_blob_custody_refuses_late_downgrade(tmp_path, monkeypatch, tool, final_mode):
    from elspeth.web.sessions.models import blob_deletion_cleanups_table, blob_replacement_cleanups_table

    entered, release = threading.Event(), threading.Event()
    primary_count = 0
    physical, phases, executed = [], [], []
    created = {}
    phase = None

    async def complete(**kwargs):
        nonlocal primary_count
        physical.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            primary_count += 1
            assert primary_count <= 3, "Blob custody triggered an extra primary request"
            if primary_count == 1:
                message = _Message(None, [_ToolCall("blob-create-actual", _Function("create_blob", json.dumps({
                    "filename": "owned.csv", "mime_type": "text/csv", "content": "value\n42\n",
                    "description": "Provider-created custody control",
                })))])
            elif primary_count == 2:
                arguments = {"blob_id": created["blob_id"]}
                if tool == "update_blob":
                    arguments["content"] = "value\n43\n"
                message = _Message(None, [_ToolCall("blob-change-actual", _Function(tool, json.dumps(arguments)))])
            else:
                assert final_mode == "auto_commit"
                message = _Message("The requested blob change is saved.")
            return _Response(choices=[_Choice(message)], usage={
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01,
            }, model=kwargs["model"], id=f"blob-primary-{primary_count}")
        assert kwargs["model"] == composer._settings.composer_advisor_model
        assert phase == "end" and phases == ["end"]
        assert final_mode == "auto_commit"
        assert kwargs.get("tools") is None and "response_format" in kwargs
        return _Response(choices=[_Choice(_Message(json.dumps({
            "verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None,
            "publication_verdict": "ADMIT",
        })))], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model=kwargs["model"], id="blob-end-1")

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Actual blob custody"}).json()["id"]

    def journals():
        with engine.connect() as connection:
            return tuple(tuple(connection.execute(select(table).where(table.c.session_id == sid)
                .order_by(*table.primary_key.columns)).all())
                for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table))
    response = client.patch(f"/api/sessions/{sid}/composer/preferences", json={
        "trust_mode": "auto_commit", "density_default": "high"})
    assert response.status_code == 200
    original_execute = tool_batch_module.execute_tool

    def execute(name, *args, **kwargs):
        executed.append(name)
        if name == tool:
            entered.set()
            assert release.wait(15)
        result = original_execute(name, *args, **kwargs)
        if name == "create_blob":
            assert result.success
            created["blob_id"] = result.data["blob_id"]
        return result

    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    async def checkpoint(**kwargs):
        nonlocal phase
        phase = kwargs["phase"]
        phases.append(phase)
        return await original_checkpoint(**kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", execute)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", checkpoint)
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(settle_sync, client, client.app, path=f"/api/sessions/{sid}/messages", body={
            "content": "Create the CSV then apply the requested blob change.",
            "state_id": None, "operation_id": str(uuid4()),
        })
        try:
            assert entered.wait(15), "Real blob change was never dispatched"
            with engine.connect() as connection:
                before = connection.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one()
            original_bytes = Path(before["storage_path"]).read_bytes()
            before_journals = journals()
            assert original_bytes == b"value\n42\n"
            response = client.patch(f"/api/sessions/{sid}/composer/preferences", json={
                "trust_mode": final_mode, "density_default": "high"})
            assert response.status_code == 200
        finally:
            release.set()
        settled = task.result(timeout=30)
    with engine.connect() as connection:
        after = connection.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one_or_none()
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).all()
        proposals = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
        chat = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)
                                  .order_by(chat_messages_table.c.sequence_no)).mappings().all()
    assert states == proposals == []
    assert executed == ["create_blob", tool]
    assert sdk.await_count == len(physical)
    audit_calls = [envelope["call"] for row in chat for envelope in row["tool_calls"] or ()
                   if envelope.get("_kind") == "llm_call_audit"]
    assert len(audit_calls) == len(physical)
    assert len({call["call_id"] for call in audit_calls}) == len(physical)
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["blob-create-actual", "blob-change-actual"]
    assert all(row["composition_state_id"] is None for row in tools)
    if final_mode == "explicit_approve":
        status, body = settled.error()
        assert status == 409 and body["detail"]["error_type"] == "composer_mutation_authority_revoked"
        assert primary_count == len(physical) == 2 and phases == []
        assert after == before
        assert journals() == before_journals
        assert Path(before["storage_path"]).read_bytes() == original_bytes
        assert "auto_mutation_revoked" in tools[-1]["content"]
    else:
        assert settled.result()
        assert primary_count == 3 and phases == ["end"] and len(physical) == 4
        if tool == "update_blob":
            assert after is not None and after["status"] == "ready"
            assert after["content_hash"] != before["content_hash"]
            assert Path(after["storage_path"]).read_bytes() == b"value\n43\n"
        else:
            assert after is None
            assert not Path(before["storage_path"]).exists()
```

Mutation controls: remove the forwarded `auto_mutation` from
`_execute_update_blob`'s `replace_blob` call, then independently from
`_execute_delete_blob`'s `_delete_blob_with_ledger` call. The corresponding late
refusal case must fail its unchanged-row/byte assertion or truthful 409
assertion, with actual fixture/provider setup successful. Restore each guard
and require both positive controls to succeed. The existing immediate
`create_blob` and inline `set_pipeline` tests remain separate controls.

### A5 complete storage/worker module

Target: `tests/unit/web/sessions/test_common_mutation_authority_workers.py`.
Do not put `pytest_plugins` in this file: explicitly import the verified
SQLite fixture, permitting the PostgreSQL collector below to override it.
The persisted candidate is synthetic storage input, explicitly marked as such;
no test claims it was authored by a provider.

```python
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.web import async_workers
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.coordination import composer_operation_authority as authority_module
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
)
from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.coordination.test_composer_operation_authority import (
    operation_store as operation_store,
    start,
)
from tests.unit.web.sessions.session_test_authority import private_evidence_for_test_turn


async def thread_event(event):
    assert await asyncio.to_thread(event.wait, 10), "Actual selected worker seam was not reached"


def reachable(error):
    """Retain objects, not merely exception classes or strings."""
    pending, result, seen = [error], [], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        result.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
        if item.__context__ is not None:
            pending.append(item.__context__)
    return result


async def captured(awaitable):
    try:
        return await awaitable
    except BaseException as error:
        return error


def coordinator_for(running):
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE,
        running.session_operation_context,
        durable_operation_id=running.claim.operation_id,
        claim_attempt=running.claim.attempt,
    ))
    ticket = coordinator.reserve(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
                                 transition_ordinal=1, semantic_ordinal=1)
    return coordinator, ticket


def p4_input(sid, running, ticket):
    from elspeth.core.canonical import canonical_json
    from elspeth.web.composer.redaction import redact_tool_call_response
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools._common import _failure_result

    context = running.session_operation_context
    request_id = "synthetic-storage-request"
    arguments = '{}'
    content = '{"success":true,"data":{"status":"SYNTHETIC_STORAGE_INPUT"}}'
    visible = VisibleToolEvent.capture(
        context=context, request_id=request_id,
        tool_call_id="storage-tool-1", tool_name="set_metadata",
        received_arguments=arguments, effective_arguments=arguments,
        selected_result=content, display_arguments=arguments, display_result=content,
    )
    row = RedactedToolRow(
        "storage-tool-1", content,
        StatePayload(CompositionStateData(metadata_={"name": "Storage candidate"})),
        visible,
    )
    base = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    failure = _failure_result(base, "Automatic mutation authority was revoked.",
                              error_code="auto_mutation_revoked", with_state_validation=False)
    refusal = canonical_json(redact_tool_call_response("set_metadata", failure.to_dict(),
                                                      telemetry=NoopRedactionTelemetry()))
    values = dict(
        session_id=str(sid), assistant_content="Synthetic storage candidate",
        raw_content="Synthetic storage candidate",
        redacted_assistant_tool_calls=({
            "id": "storage-tool-1", "type": "function",
            "function": {"name": "set_metadata", "arguments": arguments},
        },),
        redacted_tool_rows=(row,),
        revoked_tool_rows=(replace(row, content=refusal,
            composition_state_payload=None,
            visible_event=visible.with_display_result(refusal)),),
        parent_composition_state_id=None, expected_current_state_id=None,
        writer_principal="compose_loop", plugin_crash_pending=False,
        session_operation_context=context, required_work=ticket,
        mutation_ceiling=ComposerMutationCeiling(str(sid), request_id, "auto_commit"),
        authority_revocation=None,
    )
    values["private_tool_evidence"] = private_evidence_for_test_turn(values)
    return values


async def ready(store):
    _engine, _repository, _authority, service, sid = store
    await service.update_composer_preferences(
        sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(store)
    coordinator, ticket = coordinator_for(running)
    return running, coordinator, ticket, p4_input(sid, running, ticket)


def read_rows(engine, sid):
    with engine.connect() as conn:
        chat = conn.execute(select(chat_messages_table).where(
            chat_messages_table.c.session_id == str(sid)
        ).order_by(chat_messages_table.c.sequence_no)).mappings().all()
        states = conn.execute(select(composition_states_table).where(
            composition_states_table.c.session_id == str(sid))).mappings().all()
        proposals = conn.execute(select(composition_proposals_table).where(
            composition_proposals_table.c.session_id == str(sid))).mappings().all()
    return chat, states, proposals


def assert_committed_cohort(engine, sid, values, outcome):
    chat, states, proposals = read_rows(engine, sid)
    assert proposals == []
    assert [row["role"] for row in chat] == ["assistant", "tool", "audit"]
    assert len(states) == 1
    assert outcome.checkpoint_disposition == "committed"
    assert outcome.current_state_id == states[0]["id"]
    assert outcome.checkpoint is not None
    assert str(outcome.checkpoint.state.id) == states[0]["id"]
    assert chat[0]["id"] == outcome.assistant_id
    assert chat[1]["parent_assistant_id"] == chat[0]["id"]
    assert chat[1]["composition_state_id"] == states[0]["id"]
    assert chat[1]["content"] == values["redacted_tool_rows"][0].content
    assert outcome.published_tool_rows == values["redacted_tool_rows"]
    assert outcome.published_assistant_content == values["assistant_content"]
    assert outcome.authority_revocation is None
    # The writer persists the private envelope in tool_calls, not content.
    envelopes = chat[2]["tool_calls"]
    assert len(envelopes) == 1
    decoded = ComposerDiagnosticEvidence.from_envelope(
        envelopes[0], expected_session_id=str(sid),
        expected_operation_id=values["session_operation_context"].fence.operation_id,
        expected_tool_call_id="storage-tool-1")
    assert decoded == values["private_tool_evidence"][0]


@pytest.mark.asyncio
async def test_p4_cancel_before_worker_admission_retains_original_and_has_no_sql(
    operation_store, monkeypatch,
):
    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    entered = asyncio.Event()
    never_release = asyncio.Event()
    original_cancellations = []
    sync_calls = []
    original_sync = service.persist_compose_turn

    async def held_admission():
        entered.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError as original:
            original_cancellations.append(original)
            raise

    def observe_sync(**kwargs):
        sync_calls.append(kwargs)
        return original_sync(**kwargs)

    monkeypatch.setattr(async_workers, "_acquire_admission", held_admission)
    monkeypatch.setattr(service, "persist_compose_turn", observe_sync)
    task = asyncio.create_task(service.persist_compose_turn_async(**values))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel("cancel-before-physical-submission")
        result = await captured(task)
        assert len(original_cancellations) == 1
        assert result is original_cancellations[0]
        assert result.args == ("cancel-before-physical-submission",)
        assert sync_calls == []
        assert ticket.complete
        assert ticket.errors == (result,)
        coordinator.assert_completed()
        assert read_rows(engine, sid) == ([], [], [])
    finally:
        never_release.set()
        await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_ordinary_p4_cancel_while_physically_queued_never_starts_sql(
    operation_store, monkeypatch,
):
    engine, repository, _authority, service, sid = operation_store
    await service.update_composer_preferences(
        sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(operation_store)
    values = p4_input(sid, running, None)
    occupied, release, submitted = threading.Event(), threading.Event(), threading.Event()
    invocations = []
    original_sync = service.persist_compose_turn

    class WitnessExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            submitted.set()
            return future

    def occupy():
        occupied.set()
        assert release.wait(10)

    def observe_sync(**kwargs):
        invocations.append(kwargs)
        return original_sync(**kwargs)

    monkeypatch.setattr(service, "persist_compose_turn", observe_sync)
    try:
        with WitnessExecutor(max_workers=1) as executor:
            blocker = executor.submit(occupy)
            await thread_event(occupied)
            submitted.clear()
            monkeypatch.setattr(async_workers, "_get_shared_executor", lambda: executor)
            task = asyncio.create_task(service.persist_compose_turn_async(**values))
            try:
                await thread_event(submitted)
                assert invocations == []
                task.cancel("ordinary-queued-p4")
                result = await captured(task)
                assert isinstance(result, asyncio.CancelledError)
                assert not blocker.done()
                assert invocations == []
                assert read_rows(engine, sid) == ([], [], [])
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            blocker.result(timeout=10)
        # The executor is drained: a late queued write cannot escape the oracle.
        assert invocations == []
        assert read_rows(engine, sid) == ([], [], [])
    finally:
        release.set()
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_sql", [False, True])
async def test_started_p4_joins_repeated_original_cancellations_and_actual_sql(
    operation_store, monkeypatch, fail_sql,
):
    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cancellations = []
    observed = [asyncio.Event() for _ in range(3)]
    physical_outcomes = []
    physical_calls = []
    fault = OperationalError("controlled P4 graph insert", {}, RuntimeError("original driver failure"))
    original_insert = service._insert_composition_checkpoint
    original_retain = async_workers._retain_cancellation
    original_finish = async_workers.run_required_sql_finish_once

    def held_insert(*args, **kwargs):
        physical_calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        try:
            if fail_sql:
                raise fault
            return original_insert(*args, **kwargs)
        finally:
            finished.set()

    def retain(items, original):
        original_retain(items, original)
        if all(original is not prior for prior in cancellations):
            cancellations.append(original)
            observed[len(cancellations) - 1].set()

    async def observe_finish(actual_ticket, func, *args, **kwargs):
        outcome = await original_finish(actual_ticket, func, *args, **kwargs)
        physical_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_insert_composition_checkpoint", held_insert)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    monkeypatch.setattr(async_workers, "run_required_sql_finish_once", observe_finish)
    task = asyncio.create_task(service.persist_compose_turn_async(**values))
    try:
        await thread_event(entered)
        for index in range(3):
            task.cancel(f"original-p4-cancellation-{index}")
            await asyncio.wait_for(observed[index].wait(), 10)
            assert not task.done()
            assert not ticket.complete
            assert not finished.is_set()
        release.set()
        result = await captured(task)
        assert isinstance(result, BaseException)
        assert finished.is_set()
        assert len(physical_calls) == len(physical_outcomes) == 1
        assert len(cancellations) == len({id(error) for error in cancellations}) == 3
        assert [error.args for error in cancellations] == [
            (f"original-p4-cancellation-{index}",) for index in range(3)]
        retained = reachable(result)
        assert all(any(leaf is original for leaf in retained) for original in cancellations)
        handoff = physical_outcomes[0]
        assert len(handoff.deferred_cancellations) == 3
        assert all(actual is original for actual, original in zip(
            handoff.deferred_cancellations, cancellations, strict=True))
        assert ticket.complete
        coordinator.assert_completed()
        if fail_sql:
            assert type(handoff) is RequiredSQLRaised
            assert any(leaf is fault for leaf in reachable(handoff.error))
            assert any(leaf is fault for leaf in retained)
            assert read_rows(engine, sid) == ([], [], [])
        else:
            assert type(handoff) is RequiredSQLReturned
            assert_committed_cohort(engine, sid, values, handoff.value)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["stop", "p4"])
async def test_durable_stop_and_p4_obey_both_session_lock_orders(
    operation_store, monkeypatch, first,
):
    engine, repository, authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    stop_attempted, stop_locked = threading.Event(), threading.Event()
    p4_attempted, p4_locked = threading.Event(), threading.Event()
    release_first = threading.Event()
    original_stop_transaction = authority_module.locked_session_transaction
    original_p4_begin = service._session_process_locked_begin
    original_p4_lock = service._session_write_lock
    order = []

    @contextmanager
    def stop_transaction(*args, **kwargs):
        stop_attempted.set()
        with original_stop_transaction(*args, **kwargs) as conn:
            stop_locked.set()
            order.append("stop")
            if first == "stop":
                assert release_first.wait(10)
            yield conn

    @contextmanager
    def p4_begin(*args, **kwargs):
        p4_attempted.set()
        with original_p4_begin(*args, **kwargs) as conn:
            yield conn

    @contextmanager
    def p4_lock(*args, **kwargs):
        with original_p4_lock(*args, **kwargs):
            p4_locked.set()
            order.append("p4")
            if first == "p4":
                assert release_first.wait(10)
            yield

    def stop():
        return authority.request_cancel(
            session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=lambda request_id: pytest.fail(
                "A running operation must not take the queued terminal shortcut"),
        )

    monkeypatch.setattr(authority_module, "locked_session_transaction", stop_transaction)
    monkeypatch.setattr(service, "_session_process_locked_begin", p4_begin)
    monkeypatch.setattr(service, "_session_write_lock", p4_lock)
    tasks = []
    try:
        if first == "stop":
            stop_task = asyncio.create_task(asyncio.to_thread(stop))
            tasks.append(stop_task)
            await thread_event(stop_locked)
            p4_task = asyncio.create_task(service.persist_compose_turn_async(**values))
            tasks.append(p4_task)
            await thread_event(p4_attempted)
            assert not p4_locked.is_set()
        else:
            p4_task = asyncio.create_task(service.persist_compose_turn_async(**values))
            tasks.append(p4_task)
            await thread_event(p4_locked)
            stop_task = asyncio.create_task(asyncio.to_thread(stop))
            tasks.append(stop_task)
            await thread_event(stop_attempted)
            assert not stop_locked.is_set()
        assert not p4_task.done() and not stop_task.done()
        release_first.set()
        result = await captured(p4_task)
        stopped = await stop_task
        assert stopped is not None and stopped.cancel_requested_at is not None
        assert order == (["stop", "p4"] if first == "stop" else ["p4", "stop"])
        assert ticket.complete
        coordinator.assert_completed()
        if first == "stop":
            assert isinstance(result, BaseException)
            assert any(isinstance(error, ComposerOperationCancelledDuringTurn)
                       for error in reachable(result))
            assert read_rows(engine, sid) == ([], [], [])
        else:
            assert not isinstance(result, BaseException)
            assert_committed_cohort(engine, sid, values, result)
        current = authority.get(session_id=sid, operation_id=running.claim.operation_id)
        assert current is not None
        assert current.cancel_requested_at == stopped.cancel_requested_at
    finally:
        release_first.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        repository.release(running.session_operation_context)
```

### A5 complete PostgreSQL collector

Target: `tests/testcontainer/web/test_common_mutation_authority_workers_postgres.py`.
Run serially (`-m testcontainer -n 0`). The imported fixture creates an isolated
database per test and uses the existing bounded PostgreSQL test target. It does
not reuse SQLite or patch SQL locks.

```python
import pytest

from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
    operation_store as operation_store,
)
from tests.unit.web.sessions.test_common_mutation_authority_workers import (
    test_blob_custody_and_preferences_serialize_actual_filesystem_effects as test_blob_custody_and_preferences_serialize_actual_filesystem_effects,
    test_durable_stop_and_p4_obey_both_session_lock_orders as test_durable_stop_and_p4_obey_both_session_lock_orders,
    test_ordinary_p4_cancel_while_physically_queued_never_starts_sql as test_ordinary_p4_cancel_while_physically_queued_never_starts_sql,
    test_p4_cancel_before_worker_admission_retains_original_and_has_no_sql as test_p4_cancel_before_worker_admission_retains_original_and_has_no_sql,
    test_preferences_and_p4_obey_both_session_lock_orders as test_preferences_and_p4_obey_both_session_lock_orders,
    test_started_p4_joins_repeated_original_cancellations_and_actual_sql as test_started_p4_joins_repeated_original_cancellations_and_actual_sql,
)

pytestmark = pytest.mark.testcontainer
```

### A5 actual preference/P4 lock orders on SQLite and PostgreSQL

Append this test to the storage/worker module above. Import its exact same
name into `tests/testcontainer/web/test_common_mutation_authority_workers_postgres.py`
using `as test_preferences_and_p4_obey_both_session_lock_orders`, alongside
the other explicit fixture exports. Both tests use their actual SQL backend;
the wrappers only observe and hold the existing locks and return their actual
values. This is separate from durable Stop and task cancellation.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["preferences", "p4"])
async def test_preferences_and_p4_obey_both_session_lock_orders(operation_store, monkeypatch, first):
    import elspeth.web.sessions.service as session_module

    engine, repository, _authority, service, sid = operation_store
    running, coordinator, ticket, values = await ready(operation_store)
    attempted = {name: threading.Event() for name in ("preferences", "p4")}
    locked = {name: threading.Event() for name in attempted}
    release = threading.Event()
    local = threading.local()
    order = []
    original_blob_lock = session_module._blob_custody_session_lock
    original_write_lock = service._session_write_lock
    original_begin = service._session_process_locked_begin
    original_p4 = service.persist_compose_turn

    @contextmanager
    def preference_custody(*args, **kwargs):
        local.role = "preferences"
        try:
            with original_blob_lock(*args, **kwargs):
                yield
        finally:
            del local.role

    def p4(**kwargs):
        local.role = "p4"
        try:
            return original_p4(**kwargs)
        finally:
            del local.role

    @contextmanager
    def process_begin(*args, **kwargs):
        attempted[local.role].set()
        with original_begin(*args, **kwargs) as connection:
            yield connection

    @contextmanager
    def write_lock(*args, **kwargs):
        role = local.role
        with original_write_lock(*args, **kwargs):
            locked[role].set()
            order.append(role)
            if role == first:
                assert release.wait(10)
            yield

    async def preferences():
        return await service.update_composer_preferences(
            sid, trust_mode="explicit_approve", density_default="high", actor="user:alice")

    monkeypatch.setattr(session_module, "_blob_custody_session_lock", preference_custody)
    monkeypatch.setattr(service, "persist_compose_turn", p4)
    monkeypatch.setattr(service, "_session_process_locked_begin", process_begin)
    monkeypatch.setattr(service, "_session_write_lock", write_lock)
    tasks = {}
    try:
        tasks[first] = asyncio.create_task(preferences() if first == "preferences" else
                                          service.persist_compose_turn_async(**values))
        await thread_event(locked[first])
        second = "p4" if first == "preferences" else "preferences"
        tasks[second] = asyncio.create_task(preferences() if second == "preferences" else
                                           service.persist_compose_turn_async(**values))
        # SQLite can block at the process lock before SQL lock admission.
        await thread_event(attempted[second])
        assert not tasks[second].done()
        assert not locked[second].is_set()
        release.set()
        outcome = await tasks["p4"]
        await tasks["preferences"]
        assert order == [first, second]
        coordinator.assert_completed()
        assert ticket.complete
        assert (await service.get_composer_preferences(sid)).trust_mode == "explicit_approve"
        if first == "p4":
            assert_committed_cohort(engine, sid, values, outcome)
        else:
            chat, states, proposals = read_rows(engine, sid)
            assert states == proposals == []
            assert outcome.checkpoint_disposition == "empty"
            assert outcome.current_state_id is None and outcome.checkpoint is None
            assert outcome.authority_revocation.ceiling is values["mutation_ceiling"]
            assert outcome.authority_revocation.current_trust_mode == "explicit_approve"
            assert outcome.published_tool_rows == values["revoked_tool_rows"]
            assert outcome.published_assistant_content == ""
            assert [row["role"] for row in chat] == ["assistant", "tool", "audit"]
            assert chat[0]["content"] == "" and chat[0]["raw_content"] == values["raw_content"]
            assert chat[1]["content"] == values["revoked_tool_rows"][0].content
            assert chat[1]["composition_state_id"] is None
            assert chat[1]["parent_assistant_id"] == outcome.assistant_id
            actual_private = ComposerDiagnosticEvidence.from_envelope(
                chat[2]["tool_calls"][0], expected_session_id=str(sid),
                expected_operation_id=running.session_operation_context.fence.operation_id,
                expected_tool_call_id="storage-tool-1")
            assert actual_private == values["private_tool_evidence"][0]
    finally:
        release.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        repository.release(running.session_operation_context)
```

### A5 separate-process filesystem custody and preference exclusion

Append the following helper/test to the storage worker module and export
`test_blob_custody_and_preferences_serialize_actual_filesystem_effects` in the
PostgreSQL collector. It uses the actual existing file-backed SQLite fixture
or the PostgreSQL fixture's isolated database. The child creates a fresh
engine/repository; no process-local lock, connection, service or pool is
inherited. Storage inputs are explicit synthetic fixtures. The public SDK
tests above separately prove provider authorship and the tool-to-owner path.

```python
def blob_custody_child(database_url, data_dir, context, ceiling, blob_id, action, entered, release, results):
    from dataclasses import replace
    from pathlib import Path
    from uuid import UUID

    from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused
    from elspeth.contracts.enums import CreationModality
    from elspeth.web.blobs import service as blob_module
    from elspeth.web.blobs import replacement as replacement_module
    from elspeth.web.blobs.service import BlobServiceImpl, _persist_blob_content, content_hash
    from elspeth.web.blobs.replacement import BlobReplacementCoordinator
    from elspeth.web.coordination.repository import PostgresSessionOperationRepository
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.locking import _blob_custody_session_lock

    engine = create_session_engine(database_url)
    repository = (PostgresSessionOperationRepository(engine) if engine.dialect.name == "postgresql"
                  else SQLiteLocalSessionOperationAuthority(engine))
    original_write = blob_module._atomic_write_blob
    original_replace_write = replacement_module._atomic_write_blob
    original_delete = BlobServiceImpl._commit_blob_deletion_plan

    def pause():
        entered.set()
        assert release.wait(15), "Parent did not release actual filesystem custody"

    def write(*args, **kwargs):
        pause()
        return original_write(*args, **kwargs)

    def replace_write(*args, **kwargs):
        pause()
        return original_replace_write(*args, **kwargs)

    def delete(self, **kwargs):
        pause()
        return original_delete(self, **kwargs)

    blob_module._atomic_write_blob = write
    replacement_module._atomic_write_blob = replace_write
    BlobServiceImpl._commit_blob_deletion_plan = delete
    try:
        if action == "create":
            _persist_blob_content(engine=engine, data_dir=Path(data_dir), max_storage_per_session=1_000_000,
                blob_id=UUID(blob_id), session_id=context.fence.session_id, filename="custody.csv",
                content=b"value\n43\n", mime_type="text/csv", created_by="user", source_description=None,
                creation_modality=CreationModality.VERBATIM, created_from_message_id=None,
                creating_model_identifier=None, creating_model_version=None, creating_provider=None,
                creating_composer_skill_hash=None, creating_arguments_hash=None, idempotent=False,
                session_operation_context=context, session_operation_authority=repository, auto_mutation=ceiling)
        elif action == "replace":
            expected = repository.mutate(context, lambda tx: tx.blobs.read_blob(blob_id=UUID(blob_id)))
            content = b"value\n43\n"
            BlobReplacementCoordinator(engine=engine, data_dir=Path(data_dir), session_operation_authority=repository).replace_blob(
                expected=expected, replacement=replace(expected, size_bytes=len(content), content_hash=content_hash(content)),
                content=content, context=context, max_storage_per_session=1_000_000,
                accepting_proposal_id=None, auto_mutation=ceiling)
        else:
            assert action == "delete"
            blobs = BlobServiceImpl(engine, Path(data_dir), session_operation_authority=repository)
            with _blob_custody_session_lock(engine, context.fence.session_id):
                blobs._delete_blob_with_ledger(blob_id=UUID(blob_id), context=context, auto_mutation=ceiling)
    except ComposerAutoMutationRefused as refused:
        results.put(("refused", refused.revocation.current_trust_mode))
    except BaseException as error:
        results.put(("failed", type(error).__name__))
        raise
    else:
        results.put(("committed", None))
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace", "delete"])
@pytest.mark.parametrize("first", ["preferences", "blob"])
async def test_blob_custody_and_preferences_serialize_actual_filesystem_effects(
    operation_store, tmp_path, monkeypatch, action, first,
):
    import multiprocessing
    from pathlib import Path
    from uuid import uuid4

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.sessions.models import blobs_table, blob_deletion_cleanups_table, blob_replacement_cleanups_table

    engine, repository, _authority, service, sid = operation_store
    await service.update_composer_preferences(sid, trust_mode="auto_commit", density_default="high", actor="user:alice")
    _record, running = start(operation_store)
    context = running.session_operation_context
    ceiling = ComposerMutationCeiling(str(sid), "storage-custody-request", "auto_commit")
    data_dir = tmp_path / "custody-data"
    blobs = BlobServiceImpl(engine, data_dir, session_operation_authority=repository)
    blob_id = uuid4()
    if action != "create":
        seeded = await blobs.create_blob(sid, "custody.csv", b"value\n42\n", "text/csv",
                                        session_operation_context=context)
        blob_id = seeded.id

    def snapshot():
        with engine.connect() as connection:
            tables = tuple(tuple(connection.execute(select(table).where(table.c.session_id == str(sid))
                .order_by(*table.primary_key.columns)).all())
                for table in (blobs_table, blob_deletion_cleanups_table, blob_replacement_cleanups_table))
        directory = data_dir / "blobs" / str(sid)
        files = tuple((str(path.relative_to(directory)), path.read_bytes()) for path in sorted(directory.rglob("*")) if path.is_file())
        return tables, files

    before = snapshot()
    process_context = multiprocessing.get_context("spawn")
    entered, release = process_context.Event(), process_context.Event()
    results = process_context.Queue()
    preference_attempted = threading.Event()
    original_run_sync = service._run_sync

    async def observe_actual_worker(func, *args, **kwargs):
        def entered_worker():
            preference_attempted.set()
            return func(*args, **kwargs)
        return await original_run_sync(entered_worker)

    monkeypatch.setattr(service, "_run_sync", observe_actual_worker)
    preference = None
    process = process_context.Process(target=blob_custody_child, args=(
        engine.url.render_as_string(hide_password=False), str(data_dir), context, ceiling,
        str(blob_id), action, entered, release, results))
    try:
        if first == "preferences":
            await service.update_composer_preferences(sid, trust_mode="explicit_approve", density_default="high", actor="user:alice")
            release.set()
        process.start()
        if first == "blob":
            await thread_event(entered)
            preference = asyncio.create_task(service.update_composer_preferences(
                sid, trust_mode="explicit_approve", density_default="high", actor="user:alice"))
            await thread_event(preference_attempted)
            # The child holds only existing blob custody across physical I/O;
            # it holds no long session transaction while this predicate waits.
            assert not preference.done()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(preference), 0.2)
            assert (await service.get_composer_preferences(sid)).trust_mode == "auto_commit"
            release.set()
        await asyncio.to_thread(process.join, 20)
        assert not process.is_alive()
        assert process.exitcode == 0
        outcome = results.get(timeout=2)
        if preference is not None:
            await preference
        assert (await service.get_composer_preferences(sid)).trust_mode == "explicit_approve"
        after = snapshot()
        if first == "preferences":
            assert outcome == ("refused", "explicit_approve")
            assert not entered.is_set(), "Refused mutation entered its filesystem phase"
            assert after == before
        else:
            assert outcome == ("committed", None)
            assert after[0][1:] == ((), ())  # no abandoned replacement/deletion journals
            with engine.connect() as connection:
                actual = connection.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).mappings().one_or_none()
            if action == "delete":
                assert actual is None and after[1] == ()
            else:
                assert actual is not None and actual["status"] == "ready"
                assert Path(actual["storage_path"]).read_bytes() == b"value\n43\n"
                assert len(after[1]) == 1
    finally:
        release.set()
        if process.pid is not None:
            await asyncio.to_thread(process.join, 20)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        if preference is not None:
            await asyncio.gather(preference, return_exceptions=True)
        results.close()
        results.join_thread()
        repository.release(context)
```

Behavioral RED: remove only the new blob-custody lock from the preference
writer. The custody-first cases must fail the blocked preference/current-auto
assertions after the child and preference both reach their actual lock seams.
Restore it and require native exit 0 on SQLite and serial PostgreSQL. Remove
each business guard forwarding path separately; its preference-first test
must fail the unchanged tables/journals/bytes or no-filesystem-entry assertion.
Also remove only the `auto_mutation` keyword from the first
`prepare_blob_replacement` mutation, retaining the staged/commit guards, and
then independently from the first `prepare_blob_deletion` mutation while
retaining the commit guard. The corresponding preference-first test must
fail the no-filesystem-entry or retained-journal oracle. A later refusal is
not permission to have persisted an unauthorized business intent.

### A5 worker coverage boundaries

- Cancellation before physical **submission** above uses the actual admission
  wait and its no-submission ticket. It is not cancellation of an already
  submitted required Future. The existing required worker intentionally joins
  that Future even while queued; do not write an assertion that required P4
  must drop it just because execution has not begun.
- The SQL failure case raises the exact `OperationalError` object after the
  assistant INSERT and before graph INSERT. Actual transaction rollback must
  remove the entire cohort. Original driver error and all three cancellation
  objects must be reachable; a wrapper type or matching text is insufficient.
- Stop lock ordering uses the real `request_cancel`, whose running result sets
  `cancel_requested_at`. The operation remains running until its actual
  terminal owner settles it. These tests do not claim Stop itself created a
  terminal error or that required outer-loop audit was persisted afterward.
- The public-loop module below separately proves physical SDK and terminal publication obligations.

### A5 complete public common-loop cancellation module

Target: `tests/integration/web/composer/test_common_mutation_authority_cancellation.py`.
This supersedes the earlier note that public-loop cancellation code was absent.
It does not claim runtime passage: these are executable RED/GREEN requirements,
not results. The explicit compose adapter in `tests/helpers/composer_lease.py`
must accept/forward A1's `mutation_ceiling` keyword, as must its wrapped real
method. The fixture uses the existing real durable HTTP operation worker. Only
the physical LiteLLM response is scripted; all dispatch, EARLY, P4, required
work and terminal owners remain real.

```python
from __future__ import annotations

import asyncio
import json
import sys
import threading
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import pytest
from sqlalchemy.exc import OperationalError

from elspeth.core.canonical import stable_hash
from elspeth.web.composer import service as composer_module
from elspeth.web.composer import tool_batch as batch_module
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.coordination.quota_authority import token_usage_entry_from_llm_call_envelope
from tests.helpers.composer_operations import running_operation_sync
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
from tests.integration.web.composer.test_freeform_planner_failure_translation import (
    _build_app, _Response, _Choice, _Message, _ToolCall, _Function,
)
from tests.unit.web.sessions.test_common_mutation_authority_workers import (
    reachable, read_rows, thread_event,
)


def run_public_cancel_case(tmp_path, monkeypatch, *, seam, fail_sql=False, count=1):
    assert seam in {"before_tool", "started_p4"}
    assert not fail_sql or seam == "started_p4"
    entered, release = threading.Event(), threading.Event()
    cancellation_seen = [threading.Event() for _ in range(count)]
    originals, requests, response_ids, phases = [], [], [], []
    compose_errors, compose_tasks, dispatches = [], [], []
    sql_inputs, sql_returns, projected = [], [], []
    phase = None
    pipeline = None
    fault = OperationalError("public common-loop P4", {}, RuntimeError("original public driver failure"))
    request_text = "Build a CSV to JSONL pipeline."

    async def physical_sdk(**kwargs):
        requests.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            assert len(requests) == 1, "Cancellation permitted an extra primary call"
            names = {item["function"]["name"] for item in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            assert pipeline is not None
            response_id = "cancel-primary-1"
            message = _Message(None, [_ToolCall("cancel-set-pipeline-1", _Function(
                "set_pipeline", json.dumps({"pipeline": pipeline})))])
        else:
            assert seam == "started_p4", "New advisor work started after pre-tool cancellation"
            assert len(requests) == 2, "Cancellation permitted an extra physical request"
            assert kwargs["model"] == composer._settings.composer_advisor_model
            assert phase == "early" and phases == ["early"]
            assert kwargs.get("tools") is None
            assert "response_format" in kwargs
            response_id = "cancel-early-1"
            message = _Message(json.dumps({
                "verdict": "CLEAN", "category": "other", "steps": [],
                "findings": "The supplied candidate meets the requested shape.", "note": None,
            }))
        response_ids.append(response_id)
        return _Response(choices=[_Choice(message)], usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01,
        }, model=kwargs["model"], id=response_id)

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=physical_sdk)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Common cancellation"}).json()["id"]
    pipeline = _pipeline(tmp_path, sid)
    source = tmp_path / "blobs" / sid / "input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
    changed = client.patch(f"/api/sessions/{sid}/composer/preferences", json={
        "trust_mode": "auto_commit", "density_default": "high"})
    assert changed.status_code == 200, changed.text

    original_compose = composer.compose
    async def observe_compose(*args, **kwargs):
        actual_task = asyncio.current_task()
        assert actual_task is not None
        compose_tasks.append(actual_task)
        try:
            return await original_compose(*args, **kwargs)
        except BaseException as error:
            compose_errors.append(error)
            raise
    monkeypatch.setattr(composer, "compose", observe_compose)

    original_join = composer_module._await_tool_turn_with_deferred_cancellation
    async def observe_join(awaitable, *, cancellation_requested):
        original_set = cancellation_requested.set
        def observe_set():
            # Python retains the caller's handled exception across this call.
            # This is the actual cancellation caught by production, not a new one.
            original = sys.exception()
            if isinstance(original, asyncio.CancelledError) and all(
                original is not prior for prior in originals
            ):
                originals.append(original)
                assert len(originals) <= count
                cancellation_seen[len(originals) - 1].set()
            original_set()
        monkeypatch.setattr(cancellation_requested, "set", observe_set)
        return await original_join(awaitable, cancellation_requested=cancellation_requested)
    monkeypatch.setattr(composer_module, "_await_tool_turn_with_deferred_cancellation", observe_join)

    original_batch = composer._dispatch_tool_batch
    async def held_batch(**kwargs):
        if seam == "before_tool":
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            assert kwargs["cancellation_requested"].is_set()
        return await original_batch(**kwargs)
    monkeypatch.setattr(composer, "_dispatch_tool_batch", held_batch)

    original_execute = batch_module.execute_tool
    def observe_execute(name, *args, **kwargs):
        dispatches.append(name)
        return original_execute(name, *args, **kwargs)
    monkeypatch.setattr(batch_module, "execute_tool", observe_execute)

    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint
    async def observe_checkpoint(**kwargs):
        nonlocal phase
        phase = kwargs["phase"]
        phases.append(phase)
        return await original_checkpoint(**kwargs)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)

    original_p4 = sessions.persist_compose_turn_async
    async def observe_p4(**kwargs):
        sql_inputs.append(kwargs)
        result = await original_p4(**kwargs)
        sql_returns.append(result)
        return result
    monkeypatch.setattr(sessions, "persist_compose_turn_async", observe_p4)

    original_project = composer._persist_turn_audit
    async def observe_projection(**kwargs):
        result = await original_project(**kwargs)
        projected.append(result)
        return result
    monkeypatch.setattr(composer, "_persist_turn_audit", observe_projection)

    original_insert = sessions._insert_composition_checkpoint
    def held_insert(*args, **kwargs):
        assert seam == "started_p4"
        entered.set()
        assert release.wait(10)
        if fail_sql:
            raise fault
        return original_insert(*args, **kwargs)
    monkeypatch.setattr(sessions, "_insert_composition_checkpoint", held_insert)

    async def observe_running(async_client, operation):
        try:
            await thread_event(entered)
            assert len(compose_tasks) == 1
            task = compose_tasks[0]
            for index in range(count):
                task.cancel(f"public-common-cancel-{index}")
                await thread_event(cancellation_seen[index])
                assert not task.done(), "Outer cancellation escaped the selected owned boundary"
                assert not operation.drive.done()
                if seam == "started_p4":
                    assert len(sql_inputs) == 1
                    assert not sql_inputs[0]["required_work"].complete
            release.set()
            await asyncio.wait_for(asyncio.shield(operation.drive), 20)
            response = await async_client.get(operation.poll_path)
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert snapshot["status"] == "failed"
            assert snapshot["result"] is None
            assert snapshot["error"] is not None
            return snapshot
        finally:
            release.set()

    snapshot = running_operation_sync(client, client.app,
        path=f"/api/sessions/{sid}/messages", body={
            "content": request_text, "operation_id": str(uuid4()), "state_id": None,
        }, observe=observe_running)
    assert len(compose_errors) == 1
    error = compose_errors[0]
    leaves = reachable(error)
    assert len(originals) == len({id(original) for original in originals}) == count
    assert [original.args for original in originals] == [
        (f"public-common-cancel-{index}",) for index in range(count)]
    assert all(any(leaf is original for leaf in leaves) for original in originals)
    if fail_sql:
        assert any(leaf is fault for leaf in leaves)

    chat, states, proposals = read_rows(engine, sid)
    assert proposals == []
    expected_calls = 1 if seam == "before_tool" else 2
    assert sdk.await_count == len(requests) == len(response_ids) == expected_calls
    assert phases == ([] if seam == "before_tool" else ["early"])
    assert dispatches == ([] if seam == "before_tool" else ["set_pipeline"])
    envelopes = [envelope for row in chat if row["role"] == "audit"
                 for envelope in row["tool_calls"] or ()
                 if envelope.get("_kind") == "llm_call_audit"]
    assert len(envelopes) == expected_calls
    assert len({envelope["call"]["call_id"] for envelope in envelopes}) == expected_calls
    by_response = {envelope["call"]["provider_request_id"]: envelope for envelope in envelopes}
    assert set(by_response) == set(response_ids)
    for request, response_id in zip(requests, response_ids, strict=True):
        envelope = by_response[response_id]
        call = envelope["call"]
        assert call["status"] == "success"
        assert call["model_requested"] == request["model"]
        assert call["messages_hash"] == stable_hash(request["messages"])
        assert call["tools_spec_hash"] == (
            stable_hash(request["tools"]) if request.get("tools") else None)
        usage = token_usage_entry_from_llm_call_envelope(envelope)
        assert usage is not None
        assert usage.call_id == call["call_id"]
        assert usage.prompt_tokens == 10 and usage.completion_tokens == 5

    tools = [row for row in chat if row["role"] == "tool"]
    if seam == "before_tool":
        assert states == [] and tools == []
        assert sql_inputs == sql_returns == projected == []
    elif fail_sql:
        assert states == []
        assert sql_returns == projected == []
        assert len(sql_inputs) == 1 and sql_inputs[0]["required_work"].complete
        # Required terminal audit may retain the executed tool separately, but
        # it may not claim the rolled-back P4 state or original P4 assistant.
        assert all(row["composition_state_id"] is None for row in chat)
    else:
        assert len(states) == len(sql_inputs) == len(sql_returns) == len(projected) == 1
        assert sql_inputs[0]["required_work"].complete
        outcome = sql_returns[0]
        assert outcome.checkpoint_disposition == "committed"
        assert outcome.current_state_id == states[0]["id"]
        assert outcome.checkpoint is not None
        assert str(outcome.checkpoint.state.id) == states[0]["id"]
        assert projected[0].current_state_id == states[0]["id"]
        assert projected[0].persisted_assistant_message_id == outcome.assistant_id
        assert projected[0].persisted_tool_call_turn
        assert [row["tool_call_id"] for row in tools] == ["cancel-set-pipeline-1"]
        assert tools[0]["composition_state_id"] == states[0]["id"]
        assert tools[0]["parent_assistant_id"] == outcome.assistant_id
        private = [envelope for row in chat if row["role"] == "audit"
                   for envelope in row["tool_calls"] or ()
                   if envelope.get("_kind") == "composer_diagnostic_evidence"
                   and envelope["tool_call_id"] == "cancel-set-pipeline-1"]
        assert len(private) == 1
        evidence = ComposerDiagnosticEvidence.from_envelope(private[0],
            expected_session_id=sid, expected_tool_call_id="cancel-set-pipeline-1")
        assert evidence == sql_inputs[0]["private_tool_evidence"][0]
    page = client.get(f"/api/sessions/{sid}/operations", params={"operation_id": snapshot["operation_id"]})
    assert page.status_code == 200, page.text
    (failure_outcome,) = page.json()["items"]
    saved = failure_outcome["saved_revisions"]
    assert saved["count"] == (1 if seam == "started_p4" and not fail_sql else 0)
    assert saved["latest_state_id"] == (states[0]["id"] if saved["count"] else None)
    assert saved["latest_version"] == (states[0]["version"] if saved["count"] else None)
    assert failure_outcome["error_type"] == ("audit_integrity_error" if fail_sql else "composer_operation_worker_lost")
    return snapshot


def test_public_cancel_before_tool_keeps_completed_primary_and_starts_no_tool(tmp_path, monkeypatch):
    run_public_cancel_case(tmp_path, monkeypatch, seam="before_tool")


@pytest.mark.parametrize("fail_sql", [False, True])
def test_public_repeated_cancel_joins_actual_p4_and_retains_all_originals(tmp_path, monkeypatch, fail_sql):
    run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=fail_sql, count=3)
```

This fixture cancels the compose task directly without recording durable Stop. Its cancellation-only outcome is worker loss. Durable user Stop requires the actual `request_cancel` path and is qualified separately. Keep original exception identity, required-work completion, provider evidence and saved-revision assertions unchanged.

For every no-fault (`fail_sql=False`) branch, also require the actual same-operation SQL row's `cancel_requested_at is None`, and poll error `failure_code == "worker_lost"`, `error_type == "composer_operation_worker_lost"`, `http_status == 503`, in addition to the existing history category and complete physical/P4 prefix. Do not record a Stop marker or call the Stop route by default in this fixture. The `fail_sql` branch retains all original fault/rollback/required-audit priority oracles and requires its separate actual cause-graph qualification; the cancellation-only category correction does not qualify its currently written error category.

Qualify the contrasting real public Stop case separately: hold the pre-tool barrier, POST the actual operation `/cancel` route, verify `cancel_requested_at` on that same operation, then release the held boundary. Let the route's own signal cancel the owner; require terminal 499 / `request_cancelled` in poll and public history, the completed physical prefix and no new tool dispatch. Preserve the three manually injected originals in the task-cancellation test above rather than conflating those scenarios. Retain/run `tests/unit/web/sessions/test_composer_async_worker.py::test_running_stop_and_shutdown_have_distinct_durable_causes`; the lock/marker ordering tests alone do not prove this public terminal classification.

The assertions intentionally do not rely on the former helper's first-only
local variable as proof of a whole-path defect. Run these exact tests RED/GREEN
before deciding whether to change cancellation behavior. Failure before the
selected barrier, including fixture admission or SDK dialect failure, does not
qualify as the behavioral RED. Required-work and durable terminal propagation
may retain originals elsewhere in the complete path.

### A5 conditional narrow join-owner replacement

**Apply only if** the preceding public-loop tests reach their actual barriers
and demonstrate missing original cancellation/child failure identity in the
full final cause graph. A GREEN result means keep the existing helper. A static
first-only variable is not sufficient evidence to change production behavior.
This candidate changes only original-object retention at the existing join,
keeps the child shielded, and still returns the actual successful child result
before the caller adopts saved-state metadata and re-raises cancellation.

The following replaces the complete existing
`composer/service.py::_await_tool_turn_with_deferred_cancellation`; its imported
names already exist in that module. It is not installed by this planning task.

```python
async def _await_tool_turn_with_deferred_cancellation[T](
    awaitable: Awaitable[T],
    *,
    cancellation_requested: asyncio.Event,
) -> tuple[T, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(awaitable)
    deferred: list[asyncio.CancelledError] = []

    def retained_cancellation(child_error: BaseException | None = None) -> asyncio.CancelledError:
        first = deferred[0]
        originals: list[BaseException] = []
        prior = first.__cause__
        for error in (*deferred[1:], prior, child_error):
            if error is not None and error is not first and all(error is not item for item in originals):
                originals.append(error)
        if originals:
            first.__cause__ = BaseExceptionGroup("Tool turn retained original outcomes", originals)
        return first

    def raise_joined_child(child_error: BaseException) -> NoReturn:
        cancellation = retained_cancellation(child_error)
        if type(child_error) is ComposerToolFailure:
            # Keep the established evidence carrier, and also make every original
            # reachable through standard exception links for outer classifiers.
            raise ComposerToolCancellation(
                cancellation,
                tool_invocations=child_error.tool_invocations,
                llm_calls=child_error.llm_calls,
            ) from cancellation
        raise cancellation

    while True:
        try:
            result = await asyncio.shield(task)
        except _ToolBatchCancellationRequested:
            if not deferred:
                raise
            raise retained_cancellation()
        except asyncio.CancelledError as original:
            if task.cancelled():
                # Read the actual child result; shield's synthetic cancellation
                # does not replace the original child outcome/evidence carrier.
                try:
                    task.result()
                except BaseException as child_error:
                    if not deferred:
                        raise
                    raise_joined_child(child_error)
                raise AssertionError("A cancelled child returned a result")
            if all(original is not item for item in deferred):
                deferred.append(original)
            cancellation_requested.set()
            if not task.done():
                continue
            try:
                result = task.result()
            except _ToolBatchCancellationRequested:
                raise retained_cancellation()
            except BaseException as child_error:
                raise_joined_child(child_error)
            return result, retained_cancellation()
        except BaseException as child_error:
            if not deferred:
                raise
            raise_joined_child(child_error)
        else:
            return result, retained_cancellation() if deferred else None
```

Add `NoReturn` to the existing `typing` import if applying this candidate. The
public pre-tool and SQL-error tests also require actual completed primary audit
retention. If those fail while identity passes, fix the demonstrated audit
attachment at its existing owner; do not count a join-only change as solving
that separate failing assertion. In particular, do not make the test wrapper
attach call records or saved state on production's behalf.

## A6 — Continuation integration is mandatory before cold execution

The complete plan source is immutable
`170ec0ba3f801034b774b31c2193f897c7d3993d`, particularly
`docs/superpowers/plans/2026-10-21-composer-continuation-frames.md`.
Its future modules are not existing production modules. Do not create them
merely to implement this bounded repair.

- [ ] Amend main C1 `ContinuationFrameStack` with required
  `mutation_ceiling: MutationCeilingSnapshot`, bound to
  `logical.session_id` and `logical.original_user_message_id`. C1's
  `ComposeFrame` already has required `bootstrap` and `iteration` references;
  retain that shape and require their decoded ceilings to equal the stack
  ceiling. C1 `InitializeFrame` carries it through its enclosing stack before
  TITLE, including `ingress_phase="reserved"`; admission captures it once
  from owned current preferences before the first physical action. The
  C1/C2 admission seed/codec require it and the F36 initializer does not
  recapture it after TITLE. Add `mutation_ceiling` to F5
  `_ComposeIterationState`, F8 `ContinuationBootstrapSnapshot` and F9
  `ContinuationIterationSnapshot`/`ContinuationIterationValues`. The bootstrap
  value is captured once before the logical request's first physical provider
  (including initialization/title when that phase can run first). F5's
  extraction and F23's call into `_dispatch_tool_batch` must forward it.
  F9 `capture_continuation_iteration`, `iteration_values`,
  `encode_continuation_iteration`, `decode_continuation_iteration`, and F12/F14
  runtime restoration must round-trip all three fields exactly. The bootstrap
  and iteration ceiling must match the logical request/session binding.

```python
class MutationCeilingSnapshot(ContinuationValue):
    session_id: str
    request_id: str
    initial_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationCeiling:
        return ComposerMutationCeiling(self.session_id, self.request_id, self.initial_trust_mode)
```

  Add a required `mutation_ceiling: MutationCeilingSnapshot` field to those
  future snapshot types; capture with its three exact attributes, restore via
  `materialize()`. No default-to-auto, decode-time preference read, or new
  request ID. Missing field in newly written snapshot is an integrity refusal;
  historical records remain preserved and handled by their existing supported
  reader rather than silently rewritten. The current predicate is always
  evaluated against freshly authorized runtime custody and current SQL state.

- [ ] B0–B7's once-only batch/P4 snapshots must additionally retain the nominal
  P3 revocation, alternative published rows and actual P4 outcome/projection.
  Cold P4 replay consumes the retained actual SQL result; it must not repeat
  business effects, recalculate a successful commit as refused from later
  preferences, or upgrade a refused result from later auto preferences.
  F37/C4 terminal publication accepts only the actual saved state, never the
  denied `dispatch.state`. Add cold cutpoints after initial ceiling capture,
  after upgrade before second primary, after blob commit before P4 refusal,
  and after P4 commit before its projection. Each must match uninterrupted
  SQL, visible/private evidence and physical call ledger.

  Exact B6 amendments: `PreparedP4Value` adds
  `mutation_ceiling: MutationCeilingSnapshot`,
  `authority_revocation: MutationRevocationSnapshot | None`, and
  `revoked_rows_json: str`. `_P4_ROWS` validates both row tuples;
  `p4_semantic_input` includes the three new fields, so a fresh owner cannot
  choose another ceiling or alternative publication. Add this closed value:

```python
class MutationRevocationSnapshot(ContinuationValue):
    ceiling: MutationCeilingSnapshot
    current_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationRevocation:
        return ComposerMutationRevocation(self.ceiling.materialize(), self.current_trust_mode)
```

  `PersistedComposerTurnReceipt` adds `authority_revocation:
  MutationRevocationSnapshot | None`, `published_assistant_content: str | None`,
  and `published_rows_json: str`. The actual P4 SQL transaction writes these
  values in its receipt from `AuditOutcome`; B6's
  `persist_continuation_compose_turn` finish-once method forwards all three new
  prepared inputs. `decode_prepared_p4`, B6's actual receipt validator and
  `project_p4_receipt` must materialize the nominal values and reject refusal
  plus `checkpoint_disposition="committed"`. A successful receipt requires
  actual published rows/content; unavailable outcome has no published rows
  or assistant content and cannot fabricate a refusal commit. B6's cold
  projection fills `_PersistOutcome.authority_revocation` and actual display
  fields from the retained receipt; it never recomputes them from preferences.

- [ ] The G3 companion's separate `CommonProviderCostBudget` owns the eligible
  V1 request-start configured limit and actual covered-call ledger across
  iterations. It is not reset by a turn or segment.
  Its `ProviderInvocationOwner.cost_budget` is bound once with
  `bind_cost_budget()` and covers the companion's eligible original empty Web
  **lifecycle V1** request. Root's closed lifecycle decision preserves the
  exact former planner-role limits, rather than imposing legacy planner caps
  on V2. The companion owns the final exact role descriptor. Lifecycle V2
  carries only its `ContinuationProviderCostPolicy` explicit not-applicable
  legacy-policy discriminator. Actual
  V2 per-call monetary facts already live in O1/O3 `audit_call_json` and the
  O2 recorder: do not introduce a second accumulated-cost snapshot or replay
  owner. No active request changes lifecycle or resets its budget. Common
  Advisor policy and token/storage quota remain request-wide. This
  ceiling applies to all common Composer mutations and must not inherit that
  monetary eligibility restriction. Review the merged frames code once, not two independently rewritten frame
  packets. H2.3 remains gated on both authority and monetary preservation.

### A6 exact C1/F/B6 amendments

Apply these to the named future code packets when those packets are
implemented. They are amendments to their code, not imports of nonexistent
modules into the present repair. All snapshot fields below are required for
new V2 data; there is no default-to-auto decoder.

In C1 `continuation.py`, extend `MutationCeilingSnapshot` above with these
methods and add the following nominal constructor function. Import the A1
contracts in that module:

```python
@classmethod
def capture(cls, value: ComposerMutationCeiling) -> MutationCeilingSnapshot:
    if type(value) is not ComposerMutationCeiling:
        raise TypeError("Continuation capture requires an owned mutation ceiling")
    return cls(session_id=value.session_id, request_id=value.request_id,
               initial_trust_mode=value.initial_trust_mode)

def require_logical(self, logical: LogicalRequestBinding) -> None:
    self.materialize()
    if self.session_id != logical.session_id or self.request_id != logical.original_user_message_id:
        raise AuditIntegrityError("Continuation mutation ceiling changed its original request")
```

Add this capture method to `MutationRevocationSnapshot`:

```python
@classmethod
def capture(cls, value: ComposerMutationRevocation) -> MutationRevocationSnapshot:
    if type(value) is not ComposerMutationRevocation:
        raise TypeError("Continuation refusal capture requires its actual nominal value")
    return cls(ceiling=MutationCeilingSnapshot.capture(value.ceiling),
               current_trust_mode=value.current_trust_mode)
```

C1's class and its existing validator receive exactly:

```diff
@@ class ContinuationFrameStack(ContinuationValue):
     logical: LogicalRequestBinding
+    mutation_ceiling: MutationCeilingSnapshot
@@ def require_unique_effects(self) -> ContinuationFrameStack:
+        self.mutation_ceiling.require_logical(self.logical)
```

In C1 `prepare_composer_continuation_admission`, add required keyword
`initial_trust_mode: Literal["auto_commit", "explicit_approve"]`. Its actual
`ContinuationFrameStack(...)` constructor receives:

```python
mutation_ceiling=MutationCeilingSnapshot(
    session_id=str(session_id), request_id=message_id,
    initial_trust_mode=initial_trust_mode,
),
```

In storage C2's actual `admit` first-admission V2 block at original packet
line 1316, after real request preconditions and before `if seed is None`, read
the actual session column on the already held transaction connection:

```python
initial_trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(
    sessions_table.c.id == str(session_id))).scalar_one()
if initial_trust_mode not in ("auto_commit", "explicit_approve"):
    raise AuditIntegrityError("Continuation admission has an invalid current trust mode")
```

Pass `initial_trust_mode=initial_trust_mode` to its seed constructor. After
the exact seed-type check, verify the supplied/server seed against that actual
first-admission read:

```python
seed_stack = decode_continuation_snapshot(seed.snapshot_json)
seed_stack.mutation_ceiling.require_logical(seed_stack.logical)
if seed_stack.mutation_ceiling.initial_trust_mode != initial_trust_mode:
    raise AuditIntegrityError("Fresh continuation seed differs from its actual admission preference")
```

The existing-parent replay arm returns before this block; no replay reads or
replaces its original ceiling. C1/C2/F7 test seed constructors pass their
explicit fixture mode (`initial_trust_mode="auto_commit"` only where the
fixture session actually has that mode; explicit-mode cases pass explicit).
The C1 `initial_stack()` fixture adds `MutationCeilingSnapshot(session_id=...,
request_id="original", initial_trust_mode="auto_commit")` matching its actual
literal logical identity. No request payload accepts this field.

Storage C2 `append_complete_snapshot` (original storage packet 875–895)
must reject a new mode with otherwise identical logical IDs. Add to its
existing immutable-request predicate, before `database_now` or any chunk or
event insert:

```diff
@@ if (frame_stack.logical != previous_stack.logical
+        or frame_stack.mutation_ceiling != previous_stack.mutation_ceiling
         or frame_stack.policy_hash != previous_stack.policy_hash
```

Append this complete test to future
`tests/unit/web/sessions/test_continuation_cold_restore.py`. Its existing
`admit_continuation` seed helper passes the actual fixture's default
`initial_trust_mode="auto_commit"` under the preceding constructor amendment.
This drives the real transition writer and compares every table's actual rows;
it does not infer immutability from a codec accepting the same request IDs.

```python
def test_transition_cannot_change_original_mutation_ceiling(operation_store):
    from sqlalchemy import select
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent
    from elspeth.web.sessions.models import sessions_table

    engine, repository, authority, _service, sid = operation_store
    admit_continuation(operation_store)
    running = start_continuation(repository, authority)

    def all_rows():
        with engine.connect() as connection:
            return {table.name: tuple(connection.execute(select(table).order_by(*table.primary_key.columns)).all())
                    for table in sessions_table.metadata.sorted_tables}

    try:
        stored = authority.load_continuation_snapshot(running)
        original = decode_continuation_snapshot(stored.payload_json)
        assert original.mutation_ceiling.initial_trust_mode == "auto_commit"
        before = all_rows()
        changed = original.model_copy(update={"mutation_ceiling": original.mutation_ceiling.model_copy(
            update={"initial_trust_mode": "explicit_approve"})})
        # This is a well-shaped ceiling for the same request; only the actual
        # transition owner's immutable binding can reject the reset.
        encoded = encode_continuation_snapshot(changed)
        assert decode_continuation_snapshot(encoded).logical == original.logical
        with pytest.raises(ContinuationCASConflict, match="immutable"):
            authority.commit_transition(running, expected_checkpoint_id=stored.checkpoint_id,
                expected_cursor=stored.cursor, snapshot_json=encoded, chunks=(),
                event=ContinuationEvent("checkpoint", '{"phase":"ceiling-reset-test"}'))
        assert all_rows() == before
        assert authority.load_continuation_snapshot(running) == stored
        actual = authority.commit_transition(running, expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor, snapshot_json=stored.payload_json, chunks=(),
            event=ContinuationEvent("checkpoint", '{"phase":"same-ceiling-control"}'))
        assert actual.cursor == stored.cursor + 1
        assert decode_continuation_snapshot(actual.payload_json).mutation_ceiling == original.mutation_ceiling
    finally:
        repository.release(running.session_operation_context)
```

Mutation control: omit only the added immutable-ceiling comparison; the first
`pytest.raises` must fail with native exit 1 after successful setup. Restore
it and require exit 0. Run this same test through the future PostgreSQL
collector's existing `operation_store` override, serially.

F8 bootstrap exact edits:

```diff
@@ class ContinuationBootstrapSnapshot(ContinuationValue):
     logical: LogicalRequestBinding
+    mutation_ceiling: MutationCeilingSnapshot
@@ def capture_continuation_bootstrap(
     *, logical: LogicalRequestBinding, actor_user_id: str,
+    mutation_ceiling: MutationCeilingSnapshot,
@@
     snapshot = ContinuationBootstrapSnapshot(
         logical=logical, actor_user_id=actor_user_id,
+        mutation_ceiling=mutation_ceiling,
@@ def bootstrap_values(snapshot: ContinuationBootstrapSnapshot) -> ContinuationBootstrapValues:
     logical = snapshot.logical
+    snapshot.mutation_ceiling.require_logical(logical)
```

Every production bootstrap call in F18 and its authoritative F36 replacement
passes `mutation_ceiling=root.mutation_ceiling`; F7/F10 fixture calls use their
actual selected `root.mutation_ceiling`. Neither `encode_continuation_bootstrap`
nor `decode_continuation_bootstrap` needs a second hand-coded JSON field:
both already serialize/validate the whole closed model and call
`bootstrap_values`. Its canonical equality now covers this required field.

F9 exact closed iteration edits (import `MutationCeilingSnapshot`):

```diff
@@ class ContinuationIterationSnapshot(ContinuationValue):
     producer_inputs: ContinuationProducerInputs
+    mutation_ceiling: MutationCeilingSnapshot
@@ class ContinuationIterationValues:
     failed_turn: FailedTurnMetadata | None
+    mutation_ceiling: ComposerMutationCeiling
@@ def iteration_values(snapshot: ContinuationIterationSnapshot) -> ContinuationIterationValues:
         _exact_iteration_value(_FAILURE, snapshot.failed_turn_json) if snapshot.failed_turn_json is not None else None,
+        snapshot.mutation_ceiling.materialize(),
@@ def capture_continuation_iteration(
     *, producer_inputs: ContinuationProducerInputs,
+    mutation_ceiling: ComposerMutationCeiling,
@@
     snapshot = ContinuationIterationSnapshot(
         producer_inputs=producer_inputs,
+        mutation_ceiling=MutationCeilingSnapshot.capture(mutation_ceiling),
```

F5 `_ComposeIterationState` gains required
`mutation_ceiling: ComposerMutationCeiling | None` immediately after
`turn_evidence`. Its warm `_compose_loop` constructor passes the existing
`mutation_ceiling` argument; `_run_compose_iteration` reads
`mutation_ceiling = iteration_state.mutation_ceiling` next to its other live
locals. Its P3 and P4 calls use the A1 keywords verbatim. Session-bound V2
restoration refuses `None` before any call.

F14 `restore_continuation_runtime` inserts this immediately after
`values = iteration_values(iteration)`:

```python
stack.mutation_ceiling.require_logical(stack.logical)
if (original.snapshot.mutation_ceiling != stack.mutation_ceiling
        or iteration.mutation_ceiling != stack.mutation_ceiling):
    raise AuditIntegrityError("Cold bootstrap or iteration changed the original mutation ceiling")
values.mutation_ceiling.validate_context(running.session_operation_context)
```

Its `_ComposeIterationState(...)` receives
`mutation_ceiling=values.mutation_ceiling`. All four iteration capture
owners receive these exact keywords:

| Future owner and source packet seam | Added keyword |
| --- | --- |
| F9 `capture_continuation_batch_owners` | `mutation_ceiling=ctx.mutation_ceiling` after requiring exact `ComposerMutationCeiling` and equality to `stack.mutation_ceiling.materialize()` |
| F15 `capture_continuation_runtime_owners` (original frame packet 6884) | `mutation_ceiling=live.mutation_ceiling` after the same exact-type/equality check |
| F16 `initialize_continuation_composition` (7113; authoritative replacement 11986) | `mutation_ceiling=root.mutation_ceiling.materialize()` |
| F7/F9 snapshot tests (3967/5377) | `mutation_ceiling=root.mutation_ceiling.materialize()` or their explicitly constructed matching nominal fixture |

The common guard for the first two owners, placed before any encoding, is:

```python
if (type(mutation_ceiling) is not ComposerMutationCeiling
        or MutationCeilingSnapshot.capture(mutation_ceiling) != stack.mutation_ceiling):
    raise AuditIntegrityError("Continuation capture changed the request mutation ceiling")
```

Here bind `mutation_ceiling = ctx.mutation_ceiling` in the batch owner and
`mutation_ceiling = live.mutation_ceiling` in runtime capture. F9's existing
whole-model iteration codecs automatically cover the new required field.

F22.1 `continuation_batch_owners` constructs `ToolBatchContext` directly. Add
the following immediately after its existing current-preferences read; the
ceiling comes from the restored logical request, never that current read:

```python
mutation_ceiling = live.mutation_ceiling
if (type(mutation_ceiling) is not ComposerMutationCeiling
        or MutationCeilingSnapshot.capture(mutation_ceiling) != selected.stack.mutation_ceiling):
    raise AuditIntegrityError("Cold tool dispatch changed its original mutation ceiling")
selected.stack.mutation_ceiling.require_logical(selected.stack.logical)
mutation_ceiling.validate_context(bindings.session_operation_context)
review_required = not mutation_ceiling.permits_auto(preferences.trust_mode)
```

Its actual `ToolBatchContext(...)` constructor gains:

```python
mutation_ceiling=mutation_ceiling,
review_required=review_required,
```

The same context flows into A1's cap and proposal/direct dispatch branches.
Do not leave the old current-mode-only branch in this second constructor.

B0/B1 already preserve the **pre-batch** state as live `entry_state` and
closed `entry_state_json` (B1 original 517/548/821). Use those existing
owners; add no second state snapshot. Add live
`authority_revocation: ComposerMutationRevocation | None = None` and strict
`authority_revocation: MutationRevocationSnapshot | None = None`; capture and
restore use the snapshot's `capture`/`materialize` methods, preserving `None`.
The B0 continuation dispatcher copies `acc.authority_revocation` into the
batch and B1's restored `_DispatchOutcome` carries that exact nominal value.
This closes the interval between an actual blob refusal and owed cold P4.

B6 `PreparedP4Value` adds these exact required fields:

```python
mutation_ceiling: MutationCeilingSnapshot
authority_revocation: MutationRevocationSnapshot | None
revoked_rows_json: str
```

`prepare_continuation_p4_value` gains these keyword parameters:

```python
mutation_ceiling: ComposerMutationCeiling,
authority_revocation: ComposerMutationRevocation | None,
base_state: CompositionState,
```

After its preserved redaction span, calculate the actual alternate with A3's
`_revoked_graph_rows`. Its `PreparedP4Value(...)` constructor gains:

```python
mutation_ceiling=MutationCeilingSnapshot.capture(mutation_ceiling),
authority_revocation=(MutationRevocationSnapshot.capture(authority_revocation)
                      if authority_revocation is not None else None),
revoked_rows_json=canonical_json(_P4_ROWS.dump_python(
    _revoked_graph_rows(rows=redacted_tool_rows, outcomes=tool_outcomes,
                       base_state=base_state, redaction_telemetry=redaction_telemetry),
    mode="json", warnings="error", fallback=owned_json)),
```

Both B6.6 preparation calls (original 2911 and 2947) add exactly:

```python
mutation_ceiling=ctx.mutation_ceiling,
authority_revocation=actual_dispatch.authority_revocation,
base_state=batch.entry_state,
```

Require `type(ctx.mutation_ceiling) is ComposerMutationCeiling` and equality
to `stack.mutation_ceiling.materialize()` at the existing B6 entry validation.
`decode_prepared_p4` inserts before its existing `p4_semantic_input(value)`:

```python
value.mutation_ceiling.materialize()
_P4_ROWS.validate_json(value.revoked_rows_json, strict=True)
if value.authority_revocation is not None:
    value.authority_revocation.materialize()
    if value.authority_revocation.ceiling != value.mutation_ceiling:
        raise AuditIntegrityError("Retained P4 refusal changed its original ceiling")
```

The canonical object in `p4_semantic_input` gains these keys. Keep its existing
normal-row semantic projection, and project alternate rows by exactly the
same loop (including `composition_state_payload`, original payload/hash and
display hashes, excluding only current lease creator identity):

```python
"mutation_ceiling": value.mutation_ceiling.model_dump(mode="json"),
"authority_revocation": (value.authority_revocation.model_dump(mode="json")
                         if value.authority_revocation is not None else None),
"revoked_rows": semantic_revoked_rows,
```

Define `semantic_revoked_rows` with the complete loop, before the return:

```python
semantic_revoked_rows = []
for row in _P4_ROWS.validate_json(value.revoked_rows_json, strict=True):
    event = row.visible_event
    semantic_revoked_rows.append({
        "tool_call_id": row.tool_call_id, "content": row.content,
        "state": _P4_ROWS.dump_python((row,), mode="json")[0]["composition_state_payload"],
        "visible": ({"tool_call_id": event.tool_call_id, "tool_name": event.tool_name,
            "payload_json": event.payload_json, "payload_hash": event.payload_hash,
            "display_arguments_hash": event.display_arguments_hash,
            "display_result_hash": event.display_result_hash} if event is not None else None),
    })
```

B6 `persist_continuation_compose_turn` and its explicit protocol declaration
gain required `mutation_ceiling`, `authority_revocation`, `revoked_tool_rows`
with the A3 types. The actual `run_required_sql_finish_once` call forwards all
three unchanged. B6.6's call adds:

```python
mutation_ceiling=prepared.mutation_ceiling.materialize(),
authority_revocation=(prepared.authority_revocation.materialize()
                      if prepared.authority_revocation is not None else None),
revoked_tool_rows=_P4_ROWS.validate_json(prepared.revoked_rows_json, strict=True),
```

B6.4's verifier is actually named `require_selected_p4_input` in
`sessions/continuation.py`. It must bind the new **actual writer arguments**,
not merely retain them from `prepared.model_copy`. Add these required keyword
parameters to that function:

```python
mutation_ceiling: ComposerMutationCeiling,
authority_revocation: ComposerMutationRevocation | None,
revoked_tool_rows: tuple[RedactedToolRow, ...],
```

Import the A1 contract types and C1 snapshot types in that module. In
`persist_compose_turn`'s existing continuation-only call to
`require_selected_p4_input`, forward those three actual local arguments
unchanged. Immediately before its existing `actual = prepared.model_copy`
statement, require the nominal ceiling and its actual request binding:

```python
if type(mutation_ceiling) is not ComposerMutationCeiling:
    raise AuditIntegrityError("P4 writer lost the original mutation ceiling")
actual_ceiling = MutationCeilingSnapshot.capture(mutation_ceiling)
actual_ceiling.require_logical(root.logical)
mutation_ceiling.validate_context(commit.running.session_operation_context)
actual_revocation = (MutationRevocationSnapshot.capture(authority_revocation)
                     if authority_revocation is not None else None)
if actual_revocation is not None and actual_revocation.ceiling != actual_ceiling:
    raise AuditIntegrityError("P4 writer supplied a refusal from another request")
```

Add these three entries to the existing `actual.update` dictionary:

```python
"mutation_ceiling": actual_ceiling,
"authority_revocation": actual_revocation,
"revoked_rows_json": canonical_json(_P4_ROWS.dump_python(
    revoked_tool_rows, mode="json", warnings="error", fallback=owned_json)),
```

Its existing semantic equality and input SHA checks now cover actual
supplied authority and both actual publication alternatives before any row
reservation. No successful graph predicate can be smuggled through a valid
prepared input by supplying a different runtime ceiling.

B6/B7 existing fixture constructors receive these exact additions:

```diff
@@ async def selected_empty_p4(store):
     prepared_value = PreparedP4Value(assistant_content="Actual P4 intermediate status", raw_content=None,
+        mutation_ceiling=root.mutation_ceiling, authority_revocation=None, revoked_rows_json="[]",
@@ async def write_empty_p4(service, running, commit, prepared, reply):
         redacted_assistant_tool_calls=(), redacted_tool_rows=(), private_tool_evidence=(), private_tool_failures=(),
+        revoked_tool_rows=(), mutation_ceiling=prepared.mutation_ceiling.materialize(), authority_revocation=None,
@@ def test_p4_unavailable_receipt_never_invents_rows_or_a_checkpoint():
     receipt = PersistedComposerTurnReceipt(effect_key="batch:x:p4", input_sha256="a" * 64,
+        authority_revocation=None, published_assistant_content=None, published_rows_json="[]",
     prepared = PreparedP4Value(assistant_content="Actual substituted status", raw_content=None,
+        mutation_ceiling=MutationCeilingSnapshot(session_id="storage-fixture", request_id="storage-request",
+            initial_trust_mode="explicit_approve"), authority_revocation=None, revoked_rows_json="[]",
```

Import `MutationCeilingSnapshot` in those two test modules. The unavailable
projection fixture intentionally has no SQL session or business write;
explicit mode remains valid for its audit-only outcome.

B6 `PersistedComposerTurnReceipt` gains required:

```python
authority_revocation: MutationRevocationSnapshot | None
published_assistant_content: str | None
published_rows_json: str
```

Every **successful actual SQL** receipt constructor receives the selected
locals from A3, inside its existing transaction:

```python
authority_revocation=MutationRevocationSnapshot.capture(refused) if refused is not None else None,
published_assistant_content=published_assistant_content,
published_rows_json=canonical_json(_P4_ROWS.dump_python(published_tool_rows, mode="json")),
```

The existing rollback/unavailable constructor receives exactly
`authority_revocation=None, published_assistant_content=None,
published_rows_json="[]"`. Add to `actual_checkpoint_bundle`:

```python
if self.authority_revocation is not None:
    self.authority_revocation.materialize()
    if self.checkpoint_disposition in ("committed", "unavailable"):
        raise ValueError("P4 receipt invented a refused committed graph or rollback publication")
if self.checkpoint_disposition == "unavailable":
    if self.published_assistant_content is not None or self.published_rows_json != "[]":
        raise ValueError("Unavailable P4 invented published bytes")
elif self.published_assistant_content is None:
    raise ValueError("Successful P4 lost its actual published assistant bytes")
```

In `read_continuation_p4_result`, after nominal receipt/input validation, select
the **retained SQL decision**, never current preferences:

```python
expected_rows_json = (prepared.revoked_rows_json if receipt.authority_revocation is not None
                      else prepared.redacted_rows_json)
expected_assistant_content = "" if receipt.authority_revocation is not None else prepared.assistant_content
if (receipt.authority_revocation is not None
        and receipt.authority_revocation.ceiling != prepared.mutation_ceiling):
    raise AuditIntegrityError("Actual P4 refusal belongs to another logical request")
if receipt.checkpoint_disposition != "unavailable" and (
    receipt.published_rows_json != expected_rows_json
    or receipt.published_assistant_content != expected_assistant_content
):
    raise AuditIntegrityError("P4 receipt changed its selected actual publication")
```

Replace its assistant content comparison with
`assistant[0].content != receipt.published_assistant_content` and its
`expected_tools` decode with
`_P4_ROWS.validate_json(receipt.published_rows_json, strict=True)`. Keep all
actual SQL IDs, committer, private invocation/withheld hashes and existing
graph-witness checks. The receipt still describes the original full tool
cohort even when no new graph checkpoint was inserted.

In `project_p4_receipt`, decode rows from `receipt.published_rows_json`, set
`persisted_assistant_content=receipt.published_assistant_content`, and add:

```python
authority_revocation=(receipt.authority_revocation.materialize()
                      if receipt.authority_revocation is not None else None),
```

Set `persisted_assistant_matches_current_dispatch` only when assistant exists,
`prepared.assistant_row_uses_current_dispatch`, and
`receipt.authority_revocation is None`.

F23.2 `prepare_continuation_batch_classification` must project the actual P4
metadata before any failure reducer. Replace the block from its current
`raise_continuation_batch_failure(...)` through `live.failed_turn = ...` with:

```python
live = runtime.live
refused = persist.authority_revocation
live.state = (_state_from_tool_json(value.entry_state_json)
              if refused is not None else dispatch.state)
live.last_validation = (live.state.validate() if refused is not None else dispatch.last_validation)
live.last_runtime_preflight = None
live.current_state_id = persist.current_state_id
live.persisted_assistant_message_id = persist.persisted_assistant_message_id
live.persisted_assistant_content = persist.persisted_assistant_content
live.persisted_tool_call_turn = persist.persisted_tool_call_turn
live.failed_turn = persist.failed_turn
raise_continuation_batch_failure(dispatch=dispatch, persist=persist, runtime=runtime, retained=value.failure)
live.mutation_success_seen = live.mutation_success_seen or dispatch.mutation_success_observed
live.advisor_review_state = _record_advisor_repair_mutations(live.advisor_review_state, dispatch.tool_outcomes)
```

Import `_state_from_tool_json` from the existing B1 owner. F23.1 retains its
existing plugin/Advisor/retained-failure/timeout/audit-integrity/verification
precedence. Append this branch after `if verification is not None: raise
verification`, importing the A4 exception from `composer.protocol`:

```python
if persist.authority_revocation is not None:
    raise ComposerMutationAuthorityRevoked(
        revocation=persist.authority_revocation,
        llm_calls=live.recorder.llm_calls,
        failed_turn=persist.failed_turn,
        state=live.state,
        initial_version=live.initial_version,
    )
```

Thus refusal exits before repair success, elision, anti-anchor mutation,
classification SQL or another provider. In F23.3's actual SQL admission,
extend `if receipt.unwind_audit_failed or ...` with
`or receipt.authority_revocation is not None` so a caller cannot bypass the
pure reducer and classify the refused batch. F37 uses this same F23 helper;
it receives the actual receipt-derived `_PersistOutcome` and has no second
authority decision.

Append these complete tests to future `test_continuation_frames.py`; its
existing `initial_stack` fixture is amended above:

```python
@pytest.mark.parametrize("mode", ["auto_commit", "explicit_approve"])
def test_cold_codec_preserves_initial_mutation_ceiling(mode):
    original = initial_stack()
    ceiling = original.mutation_ceiling.model_copy(update={"initial_trust_mode": mode})
    original = original.model_copy(update={"mutation_ceiling": ceiling})
    cold = decode_continuation_snapshot(encode_continuation_snapshot(original))
    assert cold.mutation_ceiling == ceiling
    assert cold.mutation_ceiling.materialize().permits_auto("auto_commit") is (mode == "auto_commit")
    assert cold.mutation_ceiling.materialize().permits_auto("explicit_approve") is False

@pytest.mark.parametrize("change", ["missing", "session", "request", "mode"])
def test_cold_codec_refuses_missing_or_changed_mutation_authority(change):
    payload = json.loads(encode_continuation_snapshot(initial_stack()))
    if change == "missing":
        del payload["mutation_ceiling"]
    elif change == "session":
        payload["mutation_ceiling"]["session_id"] = "another-session"
    elif change == "request":
        payload["mutation_ceiling"]["request_id"] = "another-request"
    else:
        payload["mutation_ceiling"]["initial_trust_mode"] = "invented-auto"
    with pytest.raises((AuditIntegrityError, ValueError)):
        decode_continuation_snapshot(json.dumps(payload))
```

Add this complete actual cold P4 test to
`tests/integration/web/composer/test_continuation_driver_equivalence.py`, the
D5 driver module. D2's `ordinary_driver_script(..., early=True)` already uses
the current `set_pipeline` dialect with fresh call IDs, real discovery,
manual Advisor, EARLY, semantic and END responses. D3's `p4_retained` cut
copies the actual SQL database immediately after actual P4 result retention,
before caller projection. Both cold cases change preferences **after** the
copy: neither is permitted to reinterpret the already committed receipt.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_cold_p4_uses_original_ceiling_and_actual_publication_after_preference_change(
    operation_store, tmp_path, monkeypatch, refused,
):
    import json
    import litellm
    from sqlalchemy import select
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    import elspeth.web.composer.continuation_tool_batch as batch_projection
    from elspeth.web.sessions.models import chat_messages_table, composition_states_table
    from tests.helpers.continuation_driver import (
        DeterministicDriverSDK, DriverCutObservation, clone_admitted_database,
        fresh_driver_environment, ordinary_driver_script, recover_copied_driver_owner,
        run_driver_attempt, seed_driver_case, select_driver_state,
    )

    case = await seed_driver_case(operation_store, tmp_path / "data", empty=True, trust_mode="auto_commit")
    warm = fresh_driver_environment(clone_admitted_database(case, tmp_path / "warm.db"), case)
    script = ordinary_driver_script(case, early=True)
    cold_path = tmp_path / "cold.db"
    cut = DriverCutObservation("p4_retained", copy_to=cold_path)
    original_p4 = warm.sessions.persist_continuation_compose_turn
    changed = False

    async def change_before_actual_p4(**kwargs):
        nonlocal changed
        # Discovery's empty P4 is allowed to finish; only the actual issued
        # mutation batch is held after dispatch and before its real SQL owner.
        if refused and not changed and any(row.composition_state_payload is not None
                                           for row in kwargs["redacted_tool_rows"]):
            await warm.sessions.update_composer_preferences(case.session_id,
                trust_mode="explicit_approve", density_default="high", actor="user:alice")
            changed = True
        return await original_p4(**kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", script.complete)
            patch.setattr(warm.sessions, "persist_continuation_compose_turn", change_before_actual_p4)
            warm_retired = await run_driver_attempt(warm, script, patch, cut=cut, interrupt=False)
        assert changed is refused
        assert cut.selected is not None and cold_path.exists()
        original = decode_continuation_snapshot(cut.selected.payload_json).mutation_ceiling
        assert original.initial_trust_mode == "auto_commit"
        assert original.request_id == case.original_user_message_id
    finally:
        warm.dispose()

    cold = fresh_driver_environment(f"sqlite:///{cold_path}", case)
    replay = DeterministicDriverSDK(replies=ordinary_driver_script(case, early=True).replies,
                                   consumed=dict(cut.sdk_consumed), invocations=list(cut.sdk_prefix))
    receipts, projections = [], []
    original_read = cold.authority.read_continuation_p4_result
    original_projection = batch_projection.project_p4_receipt

    def project_actual(**kwargs):
        actual = original_projection(**kwargs)
        projections.append(actual)
        return actual

    def read_actual(*args, **kwargs):
        receipt = original_read(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    def rows():
        with cold.sessions._engine.connect() as connection:
            chat = tuple(connection.execute(select(chat_messages_table).where(
                chat_messages_table.c.session_id == str(case.session_id)).order_by(chat_messages_table.c.sequence_no)).all())
            graphs = tuple(connection.execute(select(composition_states_table).where(
                composition_states_table.c.session_id == str(case.session_id)).order_by(composition_states_table.c.version)).all())
        return chat, graphs

    try:
        copied = select_driver_state(cold)
        assert copied == cut.selected
        before_chat, before_graphs = rows()
        await cold.sessions.update_composer_preferences(case.session_id,
            trust_mode="auto_commit" if refused else "explicit_approve",
            density_default="high", actor="user:alice")
        recover_copied_driver_owner(cold)
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", replay.complete)
            patch.setattr(cold.authority, "read_continuation_p4_result", read_actual)
            patch.setattr(batch_projection, "project_p4_receipt", project_actual)
            retired = await run_driver_attempt(cold, replay, patch)
        assert retired.fence_operation_id != warm_retired.fence_operation_id
        assert retired.fence_epoch > warm_retired.fence_epoch
        # Actual receipt readback checks its historical SQL committer against
        # the newly claimed current fence; it must not mint another receipt.
        assert receipts
        receipt = receipts[0]
        assert projections
        projection = projections[0]
        assert projection.persisted_assistant_content == receipt.published_assistant_content
        assert [row.content for row in projection.redacted_tool_rows] == [
            row["content"] for row in json.loads(receipt.published_rows_json)]
        assert (receipt.authority_revocation is not None) is refused
        if refused:
            assert receipt.authority_revocation.ceiling == original
            assert receipt.authority_revocation.current_trust_mode == "explicit_approve"
            assert receipt.published_assistant_content == ""
            assert receipt.inserted_state_ids == ()
            published = json.loads(receipt.published_rows_json)
            assert any("auto_mutation_revoked" in row["content"] for row in published)
            assert all(row["composition_state_payload"] is None for row in published)
            assert replay.invocations == list(cut.sdk_prefix)
        else:
            assert receipt.checkpoint_disposition == "committed"
            assert receipt.inserted_state_ids
            assert receipt.published_assistant_content
            assert replay.invocations == script.invocations
        after_chat, after_graphs = rows()
        assert after_graphs == before_graphs
        by_id = {row.id: row for row in after_chat}
        assert all(by_id[row.id] == row for row in before_chat)
        assistant = by_id[str(receipt.assistant_id)]
        assert assistant.content == receipt.published_assistant_content
        actual_tools = [by_id[str(row_id)] for row_id in receipt.chat_message_ids
                        if by_id[str(row_id)].role == "tool"]
        assert [row.content for row in actual_tools] == [row["content"] for row in json.loads(receipt.published_rows_json)]
        final = cold.authority.get(session_id=case.session_id, operation_id=case.operation_id)
        assert final.status == ("failed" if refused else "completed")
        assert decode_continuation_snapshot(select_driver_state(cold).payload_json).mutation_ceiling == original
    finally:
        cold.dispose()
```

Run behavioral mutations independently: in `project_p4_receipt`, replace
receipt-selected rows with prepared normal rows, then replace its actual
published content with prepared prose; each refused cold test must fail its
SQL-to-publication assertions or actual failure reducer before any extra
SDK. Replacing a cold ceiling by current preferences must fail the original
ceiling assertion/immutable transition, while the committed control must
still expose the original committed graph after the later downgrade. Restore
each mutation and require native exit 0. Keep the D5 full cut matrix (ingress,
primary response, partial batch, EARLY/manual/END, P4 and publication) as the
positive cross-generation identity and no-reissued-call proof.

## A7 — Verification, bounded integration and handback

- [ ] Before executing this plan, coordinator records host/cwd/branch/HEAD,
  ownership and capacity. Use the existing environment; never install or
  rebuild a worktree venv. Define these shell variables at the actual checkout:

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
COMPOSER_LOG_DIR="$COMPOSER_WORKTREE/.claude/lanes/first-carry-20261021/common-mutation-authority"
mkdir -p "$COMPOSER_LOG_DIR"
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

  Both printed roots must be this checkout. Logs and pytest temporary roots
  stay under the lane directory, not an external temporary directory.

- [ ] Run focused checks with native exit capture. RED controls run separately
  with the expected nonzero exit and must fail their behavioral assertion,
  not collection/setup. Restore each mutant before its corresponding GREEN.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp="$COMPOSER_LOG_DIR/pytest" -o "pythonpath=$COMPOSER_WORKTREE/src $COMPOSER_WORKTREE/elspeth-lints/src" tests/unit/web/composer/test_common_mutation_authority.py tests/integration/web/composer/test_common_mutation_authority.py tests/integration/web/composer/test_common_mutation_authority_cancellation.py tests/unit/web/sessions/test_common_mutation_authority_workers.py tests/unit/web/composer/test_compose_loop_persistence.py tests/unit/web/composer/test_visible_tool_event_persistence.py tests/unit/web/composer/test_private_evidence_persistence.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_composer_operation_error_parity.py tests/unit/web/sessions/test_composer_operation_codec.py > "$COMPOSER_LOG_DIR/focused.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf '%s\n' "$COMPOSER_CHECK_EXIT" > "$COMPOSER_LOG_DIR/focused.exit"
cat "$COMPOSER_LOG_DIR/focused.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -m testcontainer -n 0 --basetemp="$COMPOSER_LOG_DIR/postgres" tests/testcontainer/web/test_common_mutation_authority_workers_postgres.py tests/testcontainer/web/test_blob_custody_lock_isolation_postgres.py tests/testcontainer/web/test_session_operation_fence_postgres.py tests/testcontainer/web/test_pipeline_rejection_required_postgres.py > "$COMPOSER_LOG_DIR/postgres.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf '%s\n' "$COMPOSER_CHECK_EXIT" > "$COMPOSER_LOG_DIR/postgres.exit"
cat "$COMPOSER_LOG_DIR/postgres.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Run touched whole-tree attribute/mock/session-writer/exception-channel
  and redaction-contract gates using the exact current CONTRIBUTING commands;
  run ruff and mypy on the affected package, and frontend codec/hook tests with
  the repository's existing package script. Re-derive writer fingerprints only
  for real changed writer sites; do not widen policy or add suppressions.
  Compare keyless trust-tier findings before/after; no agent-held signing key.
  Full pytest and full serial testcontainer qualification belong to root's
  broad integration gate after the production tree settles. Focused GREEN is
  not signing, CI, deployment or live-provider acceptance.

- [ ] Commit only after the coordinator admits the coherent authority+G3+H2.3
  packet and required tests pass. Run `git --no-optional-locks status --short`,
  inspect only owned staged paths, and `scripts/branch-safety-check.sh --intent
  commit`. Never stage lane notes/logs or unrelated staged work. Suggested
  implementation commit title: `fix(composer): preserve common mutation authority`.
  This plan-writing task performs no commit or stage.

- [ ] Final independent review checks: both trust directions; one request
  ceiling; actual locked current predicate; blob/filesystem ordering; immediate
  create exception; truthful partial effects; exact audit rows and no fake
  proposal; original cancellation/failure identity; both Stop lock orders;
  current SDK dialect and real EARLY/END calls; SQLite/PostgreSQL; cold-frame
  integration; G3 cost preservation; all retained H2.3 test intents mapped.
  Any unmet row blocks H2.3 rather than being declared obsolete.
