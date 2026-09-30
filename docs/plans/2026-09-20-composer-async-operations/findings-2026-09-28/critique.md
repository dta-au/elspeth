# Completeness critique of the 2026-09-28 re-survey

Critic seat for the six seam files in this folder: `routes.md`, `persistence.md`, `composer.md`, `frontend.md`,
`platform.md` and `plan-impact.md`. All six were read in full. This file is the critic's only write.

- **Tree.** The pin is `1effedab2`. `git rev-parse HEAD` printed `1effedab2e0af7e0…` twice during the survey. At
  write time it printed **`6cb338f2fb58976393a30357e3be700dece643ed`**, so HEAD moved by one commit:
  `6cb338f2f fix(gateway): decouple OAuth token timeout`.
  - `git diff --quiet 1effedab2 HEAD -- src tests docs/plans/2026-09-20-composer-async-operations deploy evals scripts`
    exits 0, so none of those paths changed. The control is `git diff --quiet 1effedab2 HEAD -- gateway`, which exits 1.
  - **The anchors that moved are all in `composer.md` §1.4.** They are listed in §2.1 below. Every other anchor in the
    six files, and every anchor in this file, is the same at the pin and at HEAD.
- **Binding inputs.** `panel-2026-09-28/RULINGS.md` (rulings 1–5) and `panel-2026-09-28/RECOMMENDATION.md` (B′). The
  panel directory's timestamps predate the pin, so it was read as it stands.
- **Instruments.** These are scratch scripts in the session scratchpad (`crit/`), not repo tools:
  - `show.sh file line [ctx]` prints numbered lines and reports `!! MISS` past EOF;
  - `encl.py` attributes lines to their enclosing function with an AST walk;
  - `hashprobe.py` probes the receipts codec on a strict DTO;
  - `resp.py` walks the strictness of the `MessageWithStateResponse` model tree.

  No broad test suite was run.

Sections: (a) anchor spot-check, (b) contradictions resolved against the tree, (c) what a contract writer still does
not know, with 8 gaps investigated, (d) measurements the re-based T00 must take, and (e) open questions. Sections (a)–(c)
together are the "current facts / delta / implications" for this seam. §2.1 is the delta against the pin.

---

## (a) Anchor spot-check: 44 anchors, 0 wrong at the pin

**Controls.** Each batch was resolved with `show.sh`, not by eye.

- Known-positive: `messages.py:181` printed `raise _ingress_receipt_conflict(existing_ingress)`, which is exactly what
  routes.md claims.
- Known-negative: `messages.py:9999` printed `!! MISS … (file has 1147 lines)`.

| # | Anchor (file claiming it) | Printed at the pin / HEAD | Verdict |
|---|---|---|---|
| 1 | `messages.py:142` mount (routes, platform, plan-impact) | `_inflight_tally: None = Depends(_track_compose_inflight),` | ✓ |
| 2 | `compose.py:102` mount | same text | ✓ |
| 3 | `schemas.py:154` (all) | `client_request_id: UUID` | ✓ |
| 4 | `schemas.py:162-165` | `class RecomposeRequest(_RequestModel):` … `expected_user_message_id: UUID` | ✓ |
| 5 | `service.py:970` (routes, persistence, composer) | `def _require_session_operation_context_on_connection(` | ✓ |
| 6 | `service.py:1396` | `def _insert_message_ingress_receipt(` | ✓ |
| 7 | `service.py:4680` (platform) | `self._insert_message_ingress_receipt(` (16-space indent, as the pin at `test_session_db_mutation_authority.py:19360-19361` requires) | ✓ |
| 8 | `service.py:6893` | `async def add_messages_atomic(` | ✓ |
| 9 | `service.py:3294` | `async def list_composition_proposals(` | ✓ |
| 10 | `service.py:4576` | `async def add_message_with_transcript(` | ✓ |
| 11 | `models.py:59` | `SESSION_SCHEMA_EPOCH = 71` | ✓ |
| 12 | `schema.py:45` | `_COORDINATION_HARD_CUT_EPOCH = 71` | ✓ |
| 13 | `repository.py:4845` | `def _exact_active_predicates(` | ✓ |
| 14 | `repository.py:4646` | `def acquire(` | ✓ |
| 15 | `composer/service.py:944` | `deadline = asyncio.get_event_loop().time() + self._timeout_seconds` | ✓ |
| 16 | `composer/service.py:942` | `await self._chargeable_admission.require(session_operation_context)` | ✓ |
| 17 | `planning_application.py:737` | `timeout_seconds=self._timeout_seconds,` | ✓ |
| 18 | `planning_application.py:263` | `self._timeout_seconds = settings.composer_timeout_seconds` | ✓ |
| 19 | `_helpers.py:2599` | `surface: Literal["freeform"] = "freeform"` | ✓ |
| 20 | `_helpers.py:1428` | `async def _join_freeform_owned_task[T](task: asyncio.Task[T]) -> tuple[T, asyncio.CancelledError \| None]:` | ✓ |
| 21 | `_helpers.py:2607` | `request.state.composer_durable_completed = False` | ✓ |
| 22–26 | `app.py:900/904/924/941/949` (platform lifespan slot) | `await recovery_coordinator.recover()` / `orphan_task = asyncio.create_task(` / `try:` / `await orphan_task` / `await execution_service.shutdown()` | ✓ ×5 |
| 27 | `pipeline_settlement.py:228` | `timeout_seconds=request.app.state.settings.composer_timeout_seconds,` | ✓ |
| 28 | `compose.py:158` | `if conversation_records[-1].id != body.expected_user_message_id:` | ✓ |
| 29 | `schema.py:96-110` `_REQUIRED_AUDIT_TRIGGERS` | the frozenset with exactly 11 names (read in full) | ✓ (11) |
| 30 | `models.py:1614-1635` ingress no-delete (persistence) | PG `IF EXISTS (SELECT 1 FROM sessions WHERE id = OLD.session_id) THEN RAISE` | ✓ |
| 31 | `app.py:1501-1516` stale handler | flat `{"error_type":"stale_compose_state","detail":"The session changed while the compose turn was running.","request_id":…}` | ✓ |
| 32 | `test_cross_process_composer_postgres.py:72` test-app mount | `_inflight: Annotated[None, Depends(_helpers._track_compose_inflight)],` | ✓ |
| 33 | `sessionStore.ts:1619-1645` 409 accepted arm (frontend) | the `message_already_accepted` arm, ending at `return;` | ✓ |
| 34 | `sessionStore.ts:1683-1688` | the `localFailureCode` → `"message_idempotency_conflict"` mapping | ✓ |
| 35 | `sessionStore.ts:1499-1502` | `stateId = retriedIntent ? … ?? null : get().compositionState?.id ?? null`; `clientRequestId = retriedIntent?.client_request_id ?? crypto.randomUUID()` | ✓ |
| 36 | `client.ts:963-965` | `if (stateId !== undefined) { body.state_id = stateId; }` | ✓ |
| 37 | `client.ts:985` | `body: JSON.stringify({ expected_user_message_id: expectedUserMessageId }),` | ✓ |
| 38 | `DecisionPanel.tsx:349-350` | Accept `disabled={isBusy \|\| isStale}`; Reject `disabled={isBusy}` | ✓ |
| 39 | `mutation_capabilities.py:86` (persistence) | `service._require_session_operation_context_on_connection(` | ✓ |
| 40 | `service.py:1360` per-row proof (persistence I2) | `self._require_session_write_authority_on_connection(conn, session_operation_context, session_id=session_id)` | ✓ |
| 41 | `test_session_db_mutation_authority.py:19314` retired-writer pin | `"SessionServiceImpl.add_message_with_transcript._sync": {"message_ingress_receipts"},` inside the `retired = {…}` block `:19310-19316` | ✓ |
| 42 | `session_operation_handlers.py:28-30` | `SessionOperationConflictError` → 409 `{"detail":"Session operation is already active"}` | ✓ |
| 43 | `repository.py:4668-4723` exclusive acquire (routes/persistence) | `BLOB_READ` → `_admit_blob_read`; every other kind: `FOR UPDATE` on `sessions`, then fence epoch+1 | ✓ |
| 44 | `gateway/.../core/config.py:132` (composer) | at the pin ✓; **at HEAD it is `:135`** (see §2.1) | moved |

**Minor count errors found:**

- **persistence.md §1.1** says the `_session_composer_mutation_transaction` grep gives "17 lines". In fact
  `grep -c "_session_composer_mutation_transaction(" service.py` = **16**: the def at `:1002` plus **15** call sites. The
  15 sites persistence.md lists are the complete set.
- **The docs-only line counts** (`test_freeform_route_custody.py` 641, `service.py` 7194, and `sessionStore.ts` 2716,
  confirmed by `wc -l`) are right.

---

## (b) Contradictions between the files, resolved against the tree

### b.1 Nested ranges, not contradictions

These pairs cite overlapping spans of the same code. Every one was printed, and all are valid:

- The SPA accepted-409 arm: `:1619-1645` (frontend) ⊃ `:1622-1638` (plan-impact) ⊃ `:1622-1631` (routes).
- The `app.py` stale handler: `1501-1516` / `1502-1514` / `1508-1514`. The decorator is at `:1501` and the body at
  `:1509-1516`.
- `_REQUIRED_AUDIT_TRIGGERS`: `schema.py:96-110` (persistence) vs `:96-112` (platform, plan-impact). The frozenset
  closes at `:110`.
- The testcontainer mount: `:66-76` / `:67-80` / `:72`. The dependency line is `:72`.
- The delete-guard pattern: `models.py:1614-1635` (ingress, persistence) and `:1676-1692` (receipt events,
  RECOMMENDATION/plan-impact `:1681-1692`). Both are instances of the same `IF EXISTS sessions` guard.

### b.2 Refinements, where one file read further than another

- **Where `audit_only` must be threaded.** composer.md §1.7 names the `add_messages_atomic` signature as "the natural
  place" and says the body was not read. persistence.md I2 read the body. `_write` calls `_insert_chat_message`
  (`service.py:6971`), which re-proves the Family S predicate **per row** at `:1360`.
  - **persistence wins:** a flag at the transaction entry alone would still refuse every row.
  - The minimum thread is `_session_composer_mutation_transaction` (`:1002`) →
    `_require_session_write_authority_on_connection` (`:7165`) → `_insert_chat_message` (`:1289`) → the predicate at
    `:970`.
- **composer.md §3 item 10 and Q5 ("what does `evaluate_run_diagnostics` return while a job holds COMPOSE?" — NOT
  MEASURED).** This is resolvable now.
  - The diagnostics route acquires COMPOSE (`execution/routes.py:1467-1473`), which raises `SessionOperationConflictError`
    when a live lease exists (`repository.py:4689-4699`).
  - The handler is registered app-wide (`app.py:1361` → `session_operation_handlers.py:28-30`), and composer.md itself
    read `execution/routes.py:1479-1507` and found no arm that catches it.
  - **Answer:** a 409 `{"detail":"Session operation is already active"}` while a job is `running`. A *queued* job holds
    no SOL, so diagnostics proceeds, and the job's start then requeues on conflict (F-M2).
- **composer.md §1.4's sidecar-in-path question** stays open (see (d)).
- **frontend.md §1.5** reports that `stale_compose_state` has 0 hits "over `src` and `tests`", with `app.py:1512` as
  the positive control. Its `src` is the frontend `src/`: the backend has 11 hits, for example
  `mutation_capabilities.py:202`. The claim holds for the SPA, which has no `stale_compose_state` arm.

### b.3 A real divergence: three different sites for the ruling-5 check

| File | Proposed site |
|---|---|
| routes.md §3.2 | the route/worker preamble, after the head read (`messages.py:184`) and before the ingress child (`:236`) |
| persistence.md I7 | inside `add_message_with_transcript._sync` (`service.py:4627-4697`), before `_reserve_sequence_range` |
| plan-impact.md §3.1 T05 / N06 / Q4 | the start composite in `repository.py`, after the fence advance and before the `running` CAS, rolling back and settling through `settle_unstarted` |

**Resolved against the tree.** All three sites are race-free, so the choice is a contract decision on other grounds:

1. **The head cannot move while any non-BLOB_READ SOL is held.** Every other kind is exclusive and advances the fence
   epoch (`repository.py:4655-4658` docstring; code `:4668-4723`).
2. **Every server-side head-mover is SOL-fenced.** The head-mover inventory is in (c) gap 6.
   `_insert_composition_state` proves the SOL through `_require_session_write_authority_on_connection` (Family S).
3. **So a compare anywhere after the start acquires COMPOSE and before the user row is correct**, even across separate
   transactions. routes.md §3.2 "Race window" already argues this.
4. **The PG isolation level does not break an in-transaction head read.** The session DB runs READ COMMITTED: the only
   `REPEATABLE READ` in `sessions/service.py` or `coordination/repository.py` is `service.py:1094`, a receipts read
   whose comment names the RC hazard. So a head read after `locked_session_transaction`
   (`sessions/locking.py:280-283`) takes a fresh snapshot.

**The real differentiators:**

- **Row state at refusal.** The composite site refuses before the row is `running`, so the start quad stays NULL and
  `settled_by='settle_unstarted'`. The preamble site and the `_sync` site refuse with the row already `running`.
- **Recompose coverage.** The `_sync` site covers **send only**, because recompose has no user-row transaction
  (`compose.py` has 0 `add_message_with_transcript` calls). A second site would be needed for recompose.
- **Where the code lives.** The composite site puts a head read in `repository.py`. That module already imports and
  reads `composition_states_table`: `grep -c composition_states repository.py` = 70, against the control
  `session_operation_fences_table` = 67.

---

## (c) What a contract writer still does not know: 8 gaps investigated

### Gap 1 (ruling 5): a connection-taking "expected head" check already exists, and no file names it

```python
# src/elspeth/web/sessions/service.py:6053-6087
@staticmethod
def _require_expected_current_state_on_connection(
    conn: Connection, *, session_id: str,
    expected_state_id: UUID | None, expected_state_version: int | None,
) -> None:
    """Fence a state-producing operation to the route-observed head. … An empty observation is encoded
    as ``(None, None)``; a present observation requires both exact UUID and positive version."""
```

- **Semantics.**
  - `expected_state_id is None` with a head present → refuse (`:6082-6085`). This is exactly ruling 5's "absent
    means no state".
  - A present observation must match both **id and version** (`:6086`).
  - It raises `OperationReceiptSettlementConflictError` (`protocol.py:355-357`, message "Operation expected head
    changed before settlement").
- **Caller.** Revert calls it inside its fenced settlement transaction (`service.py:5928`). The route observes the
  head before reserving (`state.py:756`, `get_current_state`) and maps the conflict to a settled receipt failure with
  `failure_code="stale_conflict"` (`state.py:788-796`). That is a working precedent for "refuse before any side effect,
  settle a terminal failure".
- **A second precedent, the owned typed base.** Pipeline proposal creation reads the head on the same connection and
  raises the class ruling 5 wants: `StaleComposeStateError` (`mutation_capabilities.py:194-211`).
  - `AbsentBase` with a head present → `"pipeline proposal absent base conflicts with current state"` (`:201-202`).
  - `PresentBase` with a different id → refuse (`:204-206`).
  - The types are `AbsentBase` at `composer/pipeline_proposal.py:63` and `PresentBase` at `:74`.
- **Absence control.** `grep -l "_require_expected_current_state_on_connection\|OperationReceiptSettlementConflictError"`
  and `grep -l "AbsentBase\|PresentBase"` over `findings-2026-09-28/*.md` and `panel-2026-09-28/*.md` both exit 1 (no
  file names either). The positive control for the same grep form, `_insert_message_ingress_receipt`, hits
  persistence.md and platform.md.
- **The contract must specify:**
  1. whether the bound base is id-only (the send wire carries only `state_id`) or `(id, version)`, with the version
     resolved at admission;
  2. whether to reuse the helper or the `AbsentBase`/`PresentBase` owned type;
  3. which exception class the worker raises. `StaleComposeStateError` matches the ruling's "today's body".
     `OperationReceiptSettlementConflictError` is receipt-named and maps to no flat 409 handler.

### Gap 2 (ruling 1 / B′): a strict request DTO needs `mode="before"` UUID parsers

- **Mechanism.** FastAPI 0.136.1 validates the body with `TypeAdapter.validate_python(value, from_attributes=True)`
  (`fastapi/_compat/v2.py:182`), not `validate_json`.
- **Measured** (`hashprobe.py`, a subclass of `_SessionOperationRequest` with `state_id: UUID | None = None`):

  | Model | Path | Result |
  |---|---|---|
  | No parser | `model_validate` with a string UUID | `REJECTED is_instance_of` |
  | No parser | `model_validate_json` | accepted |
  | With a `mode="before"` parser | both paths | accepted |

- **The live precedents** hand-parse exactly this way: `RevertStateRequest.state_id` (`schemas.py:403-408`) and
  `ForkSessionRequest.from_message_id` (`:370-375`).
- **The contract must state** that a strict `SendMessageRequest` / `RecomposeRequest` carries `mode="before"` parsers
  for `state_id` and `expected_user_message_id`. Without them, every SPA send is a 422 at cutover.

### Gap 3 (ruling 5 / B′ hash): absent and `null` `state_id` hash identically, measured

This is also `hashprobe.py`, using the **unmodified** `operation_receipt_request_hash`:

- `hash(absent) == hash(null)` → `True`;
- negative control: `hash(null) != hash(explicit state_id)` → `True`;
- only `model_fields_set` differs (`{'content','operation_id'}` vs `{'state_id','content','operation_id'}`), and the
  codec ignores it (`exclude_unset=False`, `operation_receipts.py:46-52`).

This answers the mechanical half of frontend.md Q1 and plan-impact.md Q5. Under the B′ normaliser the SPA's explicit
`null` and a CLI's omission are the **same bound request**. The semantic half, whether a caller that omits `state_id`
on a session with a head should be refused, is still the owner's (see (e) Q1).

### Gap 4 (ruling 1 FK shape): the deferrable-FK and RESTRICT-sibling precedents exist

persistence.md Q5 says "no deferrable FK precedent was checked". There is one:

- `composition_proposal_events.proposal_id` → `composition_proposals.id`, `ondelete="CASCADE", deferrable=True,
  initially="DEFERRED"` (`models.py:931-936`);
- the control is `grep -c ForeignKeyConstraint models.py` = 20; `deferrable` has exactly that 1 hit in `models.py`,
  `schema.py` and `coordination/*.py`.

SQLite FKs are enforced: `PRAGMA foreign_keys=ON` at `sessions/engine.py:103`, verified at `:150`.

plan-impact.md Q7 worries that a RESTRICT FK under the session cascade can fail a D7 delete. RESTRICT siblings under a
`sessions` CASCADE already ship:

- `composition_proposals(user_message_id, session_id)` → `chat_messages` RESTRICT (`models.py:692-697`);
- `session_operation_receipts(originating_message_id, session_id)` → `chat_messages` RESTRICT (`:750-755`);
- `session_operation_receipts(result_state_id, session_id)` → `composition_states` RESTRICT (`:756-761`).

On PG, the `IF EXISTS sessions` delete guard is proven to let the cascade through: a session delete with ingress rows
present runs at `test_add_message_transcript_postgres.py:204`, inside
`test_postgres_two_service_instances_accept_one_receipt_for_same_key_and_session_cascade` (`:128`).

**Not found:** a PG test that cascades a session holding a RESTRICT sibling row such as a proposal with
`user_message_id`. That is the measurement Q7 still needs (see (d)).

### Gap 5 (ruling 2): the positive predicate depends on terminal rows keeping the start quad

The ruling says "if any job row is bound to this fence triple". A post-terminal write can be refused only if the
**terminal** row still carries `session_operation_id`, `…_lease_token` and `…_epoch`. The old T03 keeps them:

- "Start quad KEPT (all set). `completed` requires it" (`T03.md:82-83`);
- the `failed` arm allows "all NULL (never started) or all set" (`:87-88`);
- the CHECK is at `T03.md:841-847`.

The RECOMMENDATION's terminal-arm amendment ("null only `claim_token` and `claim_expires_at`") speaks only of the
**claim** triple. It does not restate that the **SOL** quad must survive. Nor does the widened transition guard list
"clearing the SOL quad on a terminal row"; it covers only "on a running row".

**The contract must state it as load-bearing.** If a terminal settle ever nulls the quad, the positive predicate finds
no row and fails **open** for exactly the writes ruling 2 targets.

Corollaries:

- A ruling-5 refusal settled through `settle_unstarted` has a NULL quad, which is correct: it never bound a fence.
- The fence identity is four columns: `SessionOperationFence` carries `session_id`, `operation_id`, `lease_token` and
  `operation_epoch` (predicate `service.py:986-996`). The predicate must match the full triple (plus session), not only
  the `(session_id, session_operation_epoch)` index key the panel proposes for the lookup.

### Gap 6 (ruling 5 SPA guard): the server-side head-mover inventory

**Instrument.** Every `_insert_composition_state(` / `_insert_composition_checkpoint(` call in `service.py`, plus every
`insert(composition_states_table)` in `src/elspeth/web`, attributed with `encl.py`.

- Positive control: `settle_pipeline_composition_proposal._sync` (Accept) appears (`service.py:3054`).
- Only two modules insert states: `sessions/service.py` and `coordination/repository.py` (`grep -rln`).

| Mover | Writer | SOL kind / route |
|---|---|---|
| Proposal Accept | `settle_pipeline_composition_proposal._sync` `service.py:3054` | PROPOSAL (`proposals.py:286`, `:695`) |
| Interpretation resolve | `resolve_interpretation_event._sync` `:4092` | COMPOSE (`interpretation.py:134`, `:275`) |
| Revert | `revert_state_for_operation_receipt._sync` `:5999` | COMPOSE via receipt (`state.py:665`) |
| YAML import | `save_composition_state_with_interpretations` via `seed_state_from_runtime_yaml` (`state.py:858`, `:974`) from `POST /state/yaml` (`:843-847`) | COMPOSE (`state.py:881`) |
| e2e seed | `seed_state_for_e2e` (`state.py:1062`, `:1146`) | COMPOSE (`:1084`); `include_in_schema=False` |
| Compose itself | `persist_compose_turn` `:1833`, the checkpoint helper `:4906/:4928`, `save_composition_state` → `repository append_state` (`repository.py:859`) | COMPOSE |
| Interpretation pending | `_RepositoryInterpretationMutations.create_or_reconcile_pending` (`repository.py:1277`) | inside compose/import |

**What is not a mover:**

- **Proposal Reject** inserts no state: neither `reject_pipeline_composition_proposal` (`service.py:3163`) nor
  `reject_composition_proposal` (`:3345`) is in the inventory.
- **Fork** inserts into the **child** session (`settle_fork_operation_receipt._sync` `:6698`,
  `_ForkChildSessionMutations.insert_child_state` `repository.py:3956`). It does not move the parent head.

**Consequences:**

- Ruling 5's SPA guard disables Reject too. That is harmless, but the "base moved" rationale does not apply to it.
- frontend.md Q2's list of other movers omits **YAML import** and wrongly includes **fork**.
- The guard's real surface is Accept, interpretation resolve, revert and YAML import.

### Gap 7 (tutorial Build gating): server-side Run readiness is job-blind

- **Readiness.** `_require_tutorial_launch_readiness` (`tutorial_service.py:277-322`) checks only the head
  (`get_current_state`, `:286`), the launch blocker, and pending interpretations (`:307-322`). It has no compose-job
  or in-flight check.
- **Run.** `run_tutorial_pipeline` acquires an **EXECUTE** SOL (`:398-401`), which is exclusive with COMPOSE
  (`repository.py:4668-4723`). So:
  - a **running** job refuses Run with the app 409;
  - a **queued** job does not: Run takes the fence first, executes the pre-compose head, and the job then requeues
    behind it.
- **Today** that window is closed only by the SPA (`TutorialFreeformShell.tsx:143-145`, `!isComposing`). frontend.md
  item 7 shows the same-tab guard is lost on reload in the queued window.
- **Composer invariant 2.** This must **not** be fixed in the tutorial path. The question belongs to ordinary Run
  admission for every session: should `execution` Run refuse while the session has a nonterminal compose job? The
  tutorial inherits whatever ordinary Run does. No file raises the server half of this.

### Gap 8 (wire-name ruling Q1): "`operation_id`" already names two different ids on this path

- **Server-minted fence id.** `SessionOperationFence.operation_id` is minted by `_new_operation_id()` inside `acquire`
  (`repository.py:4701`). It is compared by the Family S predicate (`service.py:988`) and stored on the job as
  `session_operation_id` (`contract.md:180`).
- **Client-minted receipt key.** `_SessionOperationRequest.operation_id` (`schemas.py:77`) is the client-minted receipt
  key.
- **The hazard.** Adopting `operation_id` as the one wire name for compose (the panel majority's choice) is consistent
  with receipts. But every contract sentence, predicate and log field must keep the job's client `operation_id` and the
  bound `session_operation_id` distinct.
- **The same collision in the old contract.** Its F-B2/C2 row names the triple `session_operation_id`, … (`contract.md:46`),
  while ingress would key on the job's `operation_id`.
- **The contract should carry a glossary line.**

### Also missing from every file (owner copy question, not a design change)

Ruling 5 says the stale refusal returns "today's `stale_compose_state` body". Today's detail is
`"The session changed while the compose turn was running."` (`app.py:1513`). For a refusal made **before** the turn
runs, that sentence is false.

Two ways to resolve it:

- the handler text is reused verbatim (the ruling's literal reading);
- a job-specific detail is chosen and D4/D14 carry it.

---

## (d) Measurements the re-based T00 must still take

1. **The post-terminal write inventory under the same SOL.** This is ruling 2's precondition, and the drafts disagree
   on its breadth.
   - persistence.md §1.13 lists auto-title (`update_session_title`, the only non-audit write), progress, the CRL and SOL
     renew/release.
   - routes.md §3.3 adds the cancel-path sidecar persists.
   - T00 must drive a real turn with the auto-title branch hot and record every Family S and Family R write after the
     would-be terminal point. Use `update_session_title` as the ruling-2 negative control.
2. **Golden vectors from the unmodified codec, committed before N02 edits `operation_receipts.py:36-62`.**
   - Cover both receipt kinds, a fixed session UUID, and a strict DTO with defaulted and `None` fields.
   - There is no byte pin today: `test_operation_receipts.py:74-82` asserts only equality and inequality
     (persistence.md §1.5).
3. **A strict response round-trip.** Build a real `MessageWithStateResponse` (the whole tree is strict+forbid, measured
   by `resp.py`), serialise it to the JSON that `result_json` will hold, re-validate with the strict model, and assert
   identical `operation_receipt_response_hash` bytes. This proves B′'s "one result-hash definition".
4. **p50/p99 of `request_json` and `result_json`.** This is the panel information gap, and it re-measures F-m1
   (`COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH`) against the new DTOs: the wire-name key, the hashed `state_id`, and
   recompose's `expected_user_message_id` + `state_id`.
5. **A PG session cascade** with a running send job, its `user_message_id` set, an ingress row, and a RESTRICT sibling
   (a proposal with `user_message_id`). Gap 4 found no PG proof of the RESTRICT-sibling case. This fixes the FK actions
   for plan-impact Q7.
6. **The ruling-5 exposure census.** Count the callers that omit `state_id` on a session with a head: tests, the ACA
   observation probe (`azure_container_apps_observations.py:314`), `acceptance.sh:484`, `drive_battery.py:389`,
   `runner.py:77` and `common.sh:348`. Also count how often the SPA's `selectSession` load window
   (`sessionStore.ts:1369-1371`) can send `state_id: null`.
7. **Is the sidecar in any shipped composer path?** composer.md §1.4 marks this NOT MEASURED. Measure it, and how
   `GatewayErrorCode.UPSTREAM_TIMEOUT` classifies through `classify_provider_failure`, before D10's convergence body is
   trusted.
8. **Before and after snapshots of the writer manifest** `test_all_production_sessions_writers_are_reviewed_typed_authorities`
   (`test_session_db_mutation_authority.py:18617`, `pytest.xfail` on drift at `:18652`). persistence.md Q6 found it
   un-decorated but did not run it.
9. **The auto-title timing** under a real provider: the time from settlement to `update_session_title`. This decides
   whether "join auto-title before the terminal CAS" (routes Q1 / persistence Q1) is affordable.

---

## (e) Open questions (owner or contract), deduplicated across the six files

1. **"Absent means no state"** (frontend Q1, persistence Q2, routes Q5, plan-impact Q5). Gap 3 shows that null and
   absent are one bound request, so the choice is purely semantic: should every body without `state_id` sent to a
   session with a head be refused 409, including the ACA probe and the eval tooling?
2. **Ruling-5 site** (b.3): the start composite (pre-`running`, covers recompose, `settle_unstarted`), the worker
   preamble, or the send `_sync` transaction plus a recompose site? Should the helper be reused or the typed base
   (gap 1)? Is the base id-only or `(id, version)`?
3. **Auto-title under the positive predicate** (routes Q1, persistence Q1): join before the terminal CAS, move it out
   of the compose SOL, or classify it as audit? This is the only non-audit post-terminal writer measured.
4. **The blob-tool hole** (routes Q8, persistence I1/Q3). Family R writes (`blobs.py:1440`, `:1683`, `:1781`) bypass
   `service.py:970`. Does ruling 2 extend to `repository.mutate` for bound COMPOSE contexts?
5. **The wire name** (plan-impact Q1), with gap 8's glossary requirement.
6. **Run admission while a compose job is nonterminal** (gap 7). This is an ordinary Run question. Under invariant 2 it
   is never a tutorial one.
7. **The width of the SPA guard** (gap 6): should Reject stay disabled, given it does not move the head? Should revert,
   interpretation resolve and YAML import be added?
8. **The stale copy for a pre-turn refusal** (end of (c)).
9. **Inherited, still open:** recompose forensics / `base_state_id` (plan-impact Q6); the drain bound versus
   `_join_freeform_owned_task` (platform Q5); the P1 redefinition (platform Q1); `commit_composition_response` as dead
   code or as the R2 primitive (persistence Q4); the SOL-loss-mid-settlement terminal and the ungrouped
   `ExceptionGroup` 500 (routes Q2).

---

## 2.1 Delta between the pin and HEAD (`1effedab2..6cb338f2f`)

Only `gateway/` changed: `core/config.py` +14, `core/oauth.py` ±1, and two gateway tests. The only affected claims are
in composer.md §1.4:

| composer.md anchor at the pin | At HEAD |
|---|---|
| `GatewayConfig.request_timeout_seconds: float = 300.0` `config.py:132` | `:135` (a new `oauth_token_timeout_seconds: float = 60.0` is at `:134`) |
| env key `:81` | `_REQUEST_TIMEOUT_SECONDS` `:83` |
| loader default `:360`, parse `:359-367` | `:373`, `:372-380` |
| "applied … on the OAuth token call (`core/oauth.py:151`)" | **No longer true.** `oauth.py:151` now passes `timeout=self._config.oauth_token_timeout_seconds` (default 60 s, env `…OAUTH_TOKEN_TIMEOUT_SECONDS`). The 300 s bound now applies to the upstream POST only (`transport.py:164-170`, unchanged). |

composer.md's conclusion is unaffected: the per-provider-call cap is still the sidecar's 300 s when the sidecar is in
path.
