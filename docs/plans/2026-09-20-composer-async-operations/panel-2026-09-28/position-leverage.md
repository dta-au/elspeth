# Panel position: leverage lens

Panellist: leverage analyst (Meadows hierarchy). Tree: `release/0.8.1` @ `6506f6a7f`, read-only.
Question: where should the composer async job live, and which option puts the four invariants at
the highest-leverage point, so that future contributors cannot wear them away?

## Position

**B′: a separate `composer_async_operations` table, with a neutral shared request-identity codec (not
the receipts function itself, and not a byte-for-byte sibling). The invariants are enforced by database
rules and by the existing COMPOSE write chokepoint, not by kind-branches in code.** A precondition sits
outside options A–D: John must decide how the job's `operation_id` relates to the `client_request_id` /
`message_ingress_receipts` identity that already exists on `POST /messages`.

Confidence: **moderate-high** that B′ is better than A, C and a full D. **High** on the facts behind
the identity precondition. John decides how that precondition is resolved.

## 1. The table choice sits at Level 10. The invariants are what carry leverage.

The A/B/C/D question is a Meadows Level 10 (structure) choice, and it is not where the leverage sits.
Leverage sits in *where each invariant is enforced*: whether enforcement is on the default path, where
it holds unless someone actively removes it, or in a branch, where it holds only while nobody simplifies
the branch away. The table below maps each brief invariant to its enforcing mechanism and gives the
level under A and under B.

| Invariant | Mechanism that can enforce it | Under A (extend receipts) | Under B′ (own table) |
|---|---|---|---|
| **No side effect before `running`** | R3: the COMPOSE `SessionOperationLease` context is minted only by the start composite that marks the job `running`. Every composer write passes the chokepoint `_require_session_operation_context_on_connection` (`service.py:970`). | **Contradicted by the receipts model.** Receipts reserve *while already holding* a SOL (`routes/operation_receipts.py:239-248`: acquire, then `reserve`). Also, `in_progress ⇒ lease_token, lease_expires_at NOT NULL` (`models.py:795`, `ck_session_operation_receipts_status_bundle`). A queued, lease-free phase is illegal in that CHECK, so A has to rewrite the core bundle. | Level 5 rule. Structural: no SOL context exists before `running`, and the chokepoint refuses every session write without one. |
| **`running` is never taken over** | A running row has no lease column of its own (liveness == SOL liveness, contract §Composite start); the running bundle CHECK; the partial unique index `one_running_per_session`; the reaper settles only after acquiring SOL COMPOSE itself. | **The shared machine's default is the opposite.** `reserve_operation_receipt` takes over an expired `in_progress` row by CAS and returns `OperationReceiptTakenOver` (`operation_receipts.py:319-355`), and `taken_over` is a legal event kind (`models.py:848`). Under A, "never take over compose" becomes a kind-conditional inside the takeover path, which is Level 12 in effect: one refactor removes it. | Level 10/5: the column that takeover would compare against (`lease_expires_at` on a running row) does not exist, so a takeover CAS cannot be written without a schema change and an epoch cut. |
| **Full DTO stored** | Completed bundle CHECK (`result_schema='message_with_state.v1'`, `result_json`, `result_sha256` NOT NULL); hash and strict-DTO revalidation on read. | Receipts replay works the other way: it *rebuilds* from a locator and compares hashes (`routes/operation_receipts.py:95-105`). A `result_json` column would be nullable and mean something only for two kinds, and `_validate_row` (`operation_receipts.py:93-149`) gains more kind branches. | Level 5: the CHECK makes a completed row without the full body unrepresentable. |
| **Cancel races completion** | The terminal CAS keyed on `cancel_requested_at IS NULL`; a CHECK `completed ⇒ cancel_requested_at IS NULL` (contract Adopted deviations, "record invariants"); the terminal-immutable trigger. | A cancel-marker column would be meaningless on fork/revert rows, and every receipts reader would have to ignore it. | Level 5, same mechanism, without the dilution. |

**Gate (Level 6, information flow).** The mutation-authority gate currently proves that the receipts
writers are exact: six sites in one module, one operation authority, and no delete
(`tests/unit/architecture/test_session_db_mutation_authority.py:12190-12215`, TablePolicy at `:189-195`).
Option A has to widen that pin, so the gate that makes the receipts writers visible would then certify
a union of two lifecycles. Option B gives the job table its own exact TablePolicy and writer set. Each
gate then tells a reviewer something specific.

**Coupling through readers that already exist.** Under A, archive would break on day one. Archive reads
the receipts table directly. It refuses on any `in_progress` receipt (`SessionReceiptInProgressError`),
and it raises Tier-1 `AuditIntegrityError` when the kind is not `session_fork`/`state_revert`
(`coordination/repository.py:722-735`). So a running compose job in receipts would either trip the
Tier-1 check or, once patched, refuse archive. That contradicts D7 ("No archive refusal",
`contract.md:77`). `blobs/service.py:487-495, 525-531, 2286-2290` also read the receipts table directly
and filter by kind. Each of those filters is one more place where a future reader could forget the kind
predicate. B leaves all of them as they are.

## 2. The highest-leverage fact is outside A–D: there are now two client-minted identities for one send

Identity carries more leverage than any of the four options. The rule is "one user action ⇒ one
client-minted id ⇒ one durable record". The tree has moved since the plan was written (09-25):

- `SendMessageRequest` now carries `client_request_id: UUID` (`schemas.py:154`), on the coercing base
  `_RequestModel` (`schemas.py:66-69`). It is bound through
  `message_ingress_receipts(session_id, client_request_id) → user_message_id, requested_state_id`
  (`models.py:464-488`). `_existing_message_ingress_result` returns Accepted or Conflict by comparing
  content and state (`service.py:4508-4537`). It was added in `8630db9b8` (2026-09-27, epoch 69 per
  `schema.py:41`).
- The SPA already mints `client_request_id` and reuses it on retry (`sessionStore.ts:1502`).
- `RecomposeRequest` is now `expected_user_message_id: UUID` (`schemas.py:162-165`). The contract says
  it is "NEW (Task 10): only operation_id" on `_GuidedOperationRequest` (`contract.md:158-161`). That
  base class no longer exists. The strict base today is `_SessionOperationRequest` (`schemas.py:72-77`).
- Neither the spec nor any plan-folder file mentions `client_request_id` or `message_ingress` (grep
  returns nothing).

If T02/T10 run as written, one send carries **two** client-minted ids with overlapping replay and
conflict semantics. The job PK plus `request_hash` does exactly what the ingress receipt does. That is
the dual-acceptance shape the no-old-pathways doctrine forbids, and it sits at a higher leverage point
than the table choice. There are two coherent shapes, and **John decides between them**:

1. **The job row subsumes ingress dedup at the epoch cut.** `operation_id` is the only client id.
   `message_ingress_receipts` and `client_request_id` are removed. Consequences:
   `_acceptance_common/replica_probes.py:525,630-647` and `azure_container_apps_*` read or construct
   `client_request_id`, and the SPA retry logic at `sessionStore.ts:676-855, 1498-1631` changes.
2. **`operation_id` *is* the ingress key.** One UUID. The worker writes the ingress receipt with
   `client_request_id = operation_id`, and the ingress table stays as the user-row binding.

This panellist cannot rule between the two. The doctrine that removing debt is not the same as deleting
unfinished intent applies: the ingress receipt was a 09-27 fix for durable boundary outcomes on the
*synchronous* route. Whether the async cutover makes it redundant (queued has no side effects, and
running is never retried, so the worker never re-inserts) is exactly the question to ask John. What
this panel should settle is that **the plan cannot keep both ids**, and that T02/T10/T13 are stale
against the tree until this is decided.

## 3. Why B′ over C and over a full D

- **C (own codec, a byte-for-byte sibling)** puts the normalisation rule (strict + forbid, exclude
  `operation_id`, materialise defaults and `None`) in two copies. The rule is the invariant; the
  function is only its carrier. Two copies drift. This is the textbook case of the debt doctrine.
- **Full D (a shared core for identity, terminal replay and event log)** has to put
  "take over or never take over", "locator replay or full-body replay", and "lease column or SOL-bound
  liveness" behind flags. Those are the three places where the lifecycles *differ*. A shared core that
  holds them as parameters rebuilds A's kind-branch one level down. The genuinely shared part is small:
  PK shape, request normalisation, lower-hex hashing, and the terminal-immutable trigger pattern.
- **B′ shares only that genuinely common part.** Extract the normalisation into a neutral function
  (for example `sessions/operation_identity.py:client_operation_request_hash(*, schema, session_id,
  kind: str, request)`). Both `operation_receipt_request_hash` (`operation_receipts.py:39-53`) and the
  composer codec delegate to it, each with its own schema string. **Do not widen `OperationReceiptKind`**
  (`protocol.py:264`): it is the receipts' kind domain, checked at `operation_receipts.py:68`, and
  widening it widens receipts tree-wide.

## 4. Consequences for T02–T06

- **T02 (types and codec).**
  - Extract the neutral normaliser and make the receipts codec delegate to it. Prove the bytes are
    unchanged with a fixture hash taken before and after. The composer codec uses the schema string
    `composer-operation-request.v1`.
  - The codec rejects non-strict DTOs (`operation_receipts.py:42-43`). `SendMessageRequest` coerces
    today (`schemas.py:66-69,143`), so rebasing the DTOs onto `_SessionOperationRequest` is load-bearing.
  - Rebase the DTO section onto the tree: `client_request_id` and `expected_user_message_id` must appear
    in the request hash, or be deleted by John's identity ruling.
  - Pick one result-hash definition. `MessageWithStateResponse` is `_StrictResponse`
    (`schemas.py:234`), so `operation_receipt_response_hash` accepts it. Either use it, or pin with a
    test that `stable_hash(json.loads(result_json)) == operation_receipt_response_hash(dto)`. Two
    definitions that are never compared are a latent split.
- **T03 (schema).**
  - Keep the separate table exactly as the contract has it (no running lease column; one running row
    per session).
  - Add a **transition guard**, not only terminal immutability. On a `running` row, forbid changes to
    `kind`, `request_hash`, `actor_user_id`, the claim token, the SOL triple and `started_at`; forbid
    `running → queued`; forbid clearing `cancel_requested_at`. That makes "never taken over" a database
    rule a contributor cannot bypass in Python.
  - Negative control: mutate a test writer to reclaim a running row and confirm it goes red on both
    dialects.
  - Epoch: the next cut is **72**, not 68.
  - Its own TablePolicy. The receipts pin at `test_session_db_mutation_authority.py:12208-12213` stays
    as it is.
- **T04 (authority).**
  - Do not route anything through `reserve_operation_receipt`.
  - The reclaim CAS in `claim_next` must name `status='queued'` in its WHERE clause.
  - Pin in the writer manifest that no writer outside T05's start composite sets `status='running'`.
- **T05 (composite start).**
  - The only transition to `running`, as planned. The manifest pin above names it explicitly, so a
    second path to `running` fails the gate instead of passing review.
- **T06 (composite terminal and chokepoint).** Proposed at moderate confidence, not a claimed defect.
  - Upgrade F-B2/C2's **negative** predicate ("raise if a running bound job has a cancel marker",
    `contract.md:46`) to a **positive** one at `service.py:970`: *if any job row is bound to this fence
    triple, it must be `running` with `cancel_requested_at IS NULL`, unless `audit_only`.*
  - The reason is D12 (`contract.md:82`): a lease-close failure after the terminal CAS is logged and
    never relabels the row. A live SOL can therefore outlast a terminal job until lease expiry, and the
    negative predicate would admit a late write under that fence.
  - The positive form closes the window at the chokepoint every COMPOSE write already passes through,
    which is the highest-leverage point in the whole design.
  - No archive change is needed under B′: `repository.py:722-735` stays receipts-only, consistent
    with D7.

## 5. The strongest argument against this position

**One facility is easier to own than two.** With several developers arriving, "which table do I use
for a new client-minted operation?" gets an ambiguous answer under B. Receipts already has a reviewed
Tier-1 validator, an append-only event log with a `terminal_hash` (`models.py:825-874`,
`operation_receipts.py:151-200`), and a pinned authority. B′'s job table, as specified, has **no event
log**: reclaims and cancel history overwrite `claim_owner_instance_id` and `attempt`. Under ADR-046
that could be read as losing custody evidence that receipts would have kept.

My answer:
- The event log is a Level 6 information-flow feature and does not depend on the table. If John wants
  custody history for jobs, B′ can add a `composer_async_operation_events` table with the same pattern.
  That would argue for extracting the *event-log pattern*, not for merging lifecycles.
- Ownership ambiguity is resolved by a module docstring rule: a receipt means synchronous, SOL held
  from claim, locator replay; a job means queued, SOL bound at `running`, full-body replay. That rule is
  cheaper to keep than a kind-conditional takeover path.

## 6. Facts in the brief or plan that are wrong or stale

1. Brief: `coordination/repository.py` and `blobs/service.py` *read `session_operation_receipts_table`
   directly*; they do not call the module functions. The function callers are `service.py`,
   `routes/operation_receipts.py`, `routes/sessions.py` and `routes/composer/state.py`.
2. Contract (`contract.md:39`): "epoch 68 at plan time". The tree is at 71 (`models.py:59`,
   `schema.py:45`), so the next cut is 72.
3. Contract (`contract.md:117-118`) and spec (`:109`) cite `guided_operation_request_hash`, which has 0
   hits in `src/`. The model now is `operation_receipt_request_hash`.
4. Contract (`contract.md:158-161`): the DTO section, `_GuidedOperationRequest`, and "RecomposeRequest
   only operation_id" are stale against `schemas.py:66-77,143-165` (`client_request_id`,
   `expected_user_message_id`, `_SessionOperationRequest`).
5. The brief does not mention `message_ingress_receipts` / `client_request_id`
   (`models.py:464`, `8630db9b8`), a live client-minted identity on the same route.
6. Verified and correct: the receipts PK, the kind and status CHECKs, the single lease and attempt, the
   locators plus `response_hash`, the absence of request JSON, full result JSON, cancel marker, row
   actor and queued state, the events table columns, and epoch 71.

## Confidence Assessment

Overall: **Moderate-high**.

| Finding | Confidence | Basis |
|---|---|---|
| Under A, the default receipts path is takeover, which contradicts "never taken over" | High | `operation_receipts.py:319-355`, `models.py:795,848` |
| Under A, archive trips a Tier-1 check or refuses, contradicting D7 | High | `repository.py:722-735`, `contract.md:77` |
| The two-id problem exists on `/messages` | High | `schemas.py:154`, `models.py:464`, SPA `sessionStore.ts:1502`, no plan mention |
| Which way the two-id problem should resolve | Low (the owner decides) | Depends on the intent behind `8630db9b8` |
| The positive chokepoint predicate closes the D12 window | Moderate | Design reasoning from `contract.md:46,82` and `service.py:970-1000`; not run |
| A transition trigger is feasible on both dialects | Moderate | By analogy with the existing terminal-immutable triggers; not prototyped |

## Information Gaps

- The design notes for `8630db9b8` (`docs/plans/2026-09-27-composer-system-boundary-*.md`) were not
  read. They would show whether ingress receipts were meant to outlive the async cutover.
- I did not verify whether every composer write path goes through `service.py:970`. F-B2/C2 says
  "measured", but I did not re-measure the 11 call sites.
- The terminal-immutable trigger inventory ("5 places") and the PG reflection behaviour of a transition
  trigger were not verified.
- Whether ADR-046 treats job custody history (reclaims, cancels) as audit evidence is an owner call.

## Caveats and Required Follow-ups

1. **Before T02:** get John's ruling on the relationship between `client_request_id` and `operation_id`,
   and rebase the contract DTO section onto `schemas.py` as it is now.
2. Before T03: decide whether the job table gets an event log. If it does, reuse the receipts events
   *pattern*, not the table.
3. T06: prototype the positive predicate, and run a negative control that writes under a lingering SOL
   after the terminal CAS and must go red.
4. This analysis does not cover capacity or throughput, frontend custody, or the reaper's timing.
