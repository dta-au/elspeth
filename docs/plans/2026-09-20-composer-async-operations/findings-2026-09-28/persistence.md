# Re-survey: sessions persistence after the split (composer async operations)

Read-only explorer lane. Tree: main checkout, `release/0.8.1`, `git rev-parse HEAD` =
`1effedab2e0af7e09a5e8b30c66bc46ceaa130d5` at start and at end (== pinned `1effedab2`; no anchor moved).
`git status --short src tests` was empty, so every `src/`/`tests/` anchor below is HEAD == working tree.
Docs anchors (`CHANGELOG.md`, runbooks) are working-tree positions.

Inputs read: spec `docs/specs/2026-09-16-composer-async-operations-design.md` (its guided text is stale —
guided removed in `7001600fe`), `panel-2026-09-28/RULINGS.md`, `panel-2026-09-28/RECOMMENDATION.md`,
`contract.md`, and old `findings/schema-persistence.md` (measured at `d479eb2b4`; used only as a map).

Instrument notes. "read" = I read the code at the anchor. "grep" = counted by a grep whose positive control
is named beside it. `service.py` shrank from ~15k lines (old findings) to **7194** lines (`wc -l`), so
**no old `service.py` anchor survives** — every one below was re-measured. An AST helper (enclosing
function per line) was used to attribute call sites; it is a scratch script, not a repo tool.

---

## 1. CURRENT FACTS

### 1.1 Every DB-side COMPOSE-context proof a turn write passes through (fence-predicate site table)

There are **two separate proof families**. They must be named separately because the positive fence
predicate (ruling 2) can only live in one of them.

**Family S — service-side SELECT proof (no fence-row write).** One primitive:

`src/elspeth/web/sessions/service.py:970` (read)
```python
def _require_session_operation_context_on_connection(
    self, conn: Connection, context: SessionOperationContext, *,
    session_id: str, expected_kind: SessionOperationKind, now: datetime,
) -> None:
```
Body `:977-996`: exact-type check on the context, then `SELECT session_operation_fences.session_id WHERE
session_id, operation_id, lease_token, operation_epoch, operation_kind == expected_kind.value,
released_at IS NULL, lease_expires_at > now`; `None` → `SessionOperationFenceLost(TOKEN_MISMATCH)`.
Nothing else is checked — no job table, no cancel marker (grep `audit_only` over `src`: 0 hits;
grep `composer_async_operations|ComposerAsyncOperation` over `src`: 0 hits; positive control for the
same grep form: `session_operation_receipts` → 40 hits in `models.py`).

Reached by (AST-attributed; grep of the symbol gives exactly these call lines):

| Entry | Anchor | What it guards |
|---|---|---|
| `_session_composer_mutation_transaction` (contextmanager, entry proof) | `service.py:1002-1030` (call `:1014`) | every "ordinary composer" transaction below |
| `_SessionComposerMutationState._require_exact` (per-write re-proof) | `mutation_capabilities.py:83-93` (call `:86`) | each `_SessionComposerMutations` proposal write |
| `_require_session_write_authority_on_connection` (per-row proof, COMPOSE/PROPOSAL/SESSION_FORK) | `service.py:7165-7194` (call `:7188`) | its three callers: `_insert_chat_message` `:1360`, `_insert_message_ingress_receipt` `:1414`, `_insert_composition_state` `:1554` |
| `reserve_operation_receipt._sync` | `service.py:1063` | receipt reserve (COMPOSE for revert, SESSION_FORK for fork) |
| `require_operation_receipt_authority_on_connection` | `service.py:1109-1121` (call `:1117`) | receipt renew/bind/complete/fail `:1134/:1151/:1171/:1192`, revert `:5925` |
| `_insert_composition_checkpoint` | `service.py:4906` (call `:4921`) | checkpoint state insert |
| `save_composition_state_with_interpretations._sync` | `service.py:5047` | state + interpretations |
| `record_token_usage._sync` | `service.py:5420` | auto-title / run usage (non-cohort) |
| `_begin_provider_attempt_sync` | `service.py:5469` (call `:5481`) | provider **admission** row before dispatch |
| `cancel_undispatched_provider_attempt._sync` | `service.py:5543` (call `:5545`) | zero-use settle + audit row |
| `_settle_provider_attempt_sync` | `service.py:5602` (call `:5614`) | provider settlement ledger |

`_session_composer_mutation_transaction` call sites (grep, 17 lines; enclosing function by AST):
`persist_compose_turn` `:1763`, `update_session_title._sync` `:2148`, `create_composition_proposal._sync`
`:2671`, `create_pipeline_composition_proposal._sync` `:2759`, `settle_pipeline_composition_proposal._sync`
`:2887`, `reject_pipeline_composition_proposal._sync` `:3188`, `reject_composition_proposal._sync` `:3368`,
`accept_composition_proposal._sync` `:3435`, `has_applied_blob_proposal_effect._sync` `:3467`,
`resolve_interpretation_event._sync` `:3768`, `add_message._sync` `:4447`, `lookup_message_ingress._sync`
`:4559`, `add_message_with_transcript._sync` `:4631`, `commit_composition_response._sync` `:5107`,
`add_messages_atomic._sync` `:7006`.

Also Family S but SESSION_FORK-only (not a compose-turn path): `_require_session_fork_session_fences_on_connection`
`service.py:900-925`.

**Family R — repository-side UPDATE CAS on the fence row.** One predicate builder:

`src/elspeth/web/coordination/repository.py:4845` (read)
```python
@staticmethod
def _exact_active_predicates(context: SessionOperationContext, database_now: datetime) -> tuple[ColumnElement[bool], ...]:
```
(same seven predicates as Family S). Users (AST-attributed): `_compare_and_swap_on_connection` `:4889`
(→ `compare_and_swap` `:4993`, `mutate` `:5144`, `mutate_fork_creation` `:5411`, `archive_delete` `:5540`),
`renew` `:4954`, `_validate_fork_child_lease_locked` `:4996`, `renew_fork_child_lease` `:5067`,
`release` `:5510`, `reconcile_archive_delete` `:5557`, `classify_archive_manifest` `:5583`.
`SessionOperationLease.adopt` (`coordination/lifecycle.py:426-504`, read) runs
`run_sync_in_worker(authority.compare_and_swap, context)` — i.e. Family R.

A compose turn reaches Family R only through blob tools: `composer/tools/blobs.py:1433-1441`
`_require_blob_tool_authority` calls `authority.compare_and_swap(operation)` (`:1440`), then
`authority.mutate(...)` at `:1683` (create) and `:1781` (delete) (read). The `_Repository*Mutations`
facets do **not** re-select the fence (read `repository.py:507-583`); they rely on the enclosing proof.

Not SOL-fenced (grep `session_operation` over `coordination/composer_progress_authority.py`: 0 hits;
positive control `session_operation_fences_table` in `repository.py`: 67 hits): progress snapshot
publish and the inflight/CRL rows.

### 1.2 The audit-cohort write path — `add_messages_atomic`

`service.py:6893-7023` (read). Signature:
```python
async def add_messages_atomic(self, session_id: UUID, drafts: Sequence[AuditMessageDraft], *,
    writer_principal: ChatMessageWriterPrincipal, composition_state_id: UUID | None = None,
    session_operation_context: SessionOperationContext,
    session_operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE) -> None
```
- Body is a **nested closure** `_write(conn)` `:6945-7000` inside `_sync` `:7002-7013`
  (`_session_process_locked_begin` + `_session_write_lock` + `_session_composer_mutation_transaction`),
  run via `_run_sync_with_post_commit_projection` `:7023` (def `:6805`) with telemetry projection `:7015-7021`.
  **No connection-parameterised method exists** — the composite terminal cannot call it on its own
  connection without extracting `_write`.
- Per row it calls `_insert_chat_message` `:6971` → which re-proves Family S at `:1360` for **every row**.
  So an `audit_only` bypass at the transaction entry alone is not enough.
- Same transaction also writes `record_token_usage_on_connection(..., source="composer")` `:6995` and
  `mark_session_updated` `:6998` (repository `_RepositorySessionMutations.mark_session_updated`,
  `repository.py:617`).
- Filters already-checkpointed envelopes via `uncheckpointed_envelopes` `:6962`.

Callers (grep `add_messages_atomic(`, 5 lines): `routes/_helpers.py:1969`, `:2066` (`_persist_llm_calls`,
def `:2028`), `:2175` (`_persist_turn_audit_cohort`, def `:2102`), `composer/planning_application.py:371`,
`service.py:5521` (`finish_provider_attempt`, def `:5515`, per provider call via
`composer/provider_quota.py:62`).

Other chat-row insert sites (grep `_insert_chat_message(`, AST-attributed): `_insert_transition_assistant`
`:1443`, `persist_compose_turn` `:1798/:1843`, `add_message._write` `:4420`,
`add_message_with_transcript._sync` `:4662`, `cancel_undispatched_provider_attempt` `:5552`,
`revert_state_for_operation_receipt._sync` `:6022`, `add_messages_atomic._write` `:6971`.

### 1.3 Pending-proposals read (for the composite terminal)

- `service.py:3294-3343` `list_composition_proposals(self, session_id: UUID, *, status: ProposalLifecycleStatus | None = None) -> list[CompositionProposalRecord]`
  (read). Nested `_sync` opens its **own** `self._engine.connect()` (`:3308`) and does: SELECT proposals,
  SELECT `proposal.created` events, per-row `_classify_authoritative_composition_proposal`,
  `_verify_pipeline_lifecycle_authority(conn, ...)`, `_pipeline_public_metadata`.
- The per-row helpers are module functions in `sessions/proposal_authority.py` that already take a
  connection (imported by `mutation_capabilities.py:36-42`).
- **Connection-parameterised variant: not found.** grep
  `_list_composition_proposals_on_connection|def list_composition_proposals_on` over `src` → 0
  (positive control: `def list_composition_proposals` → 1 in `service.py`).
- Route consumer: `routes/_helpers.py:360-365` `_pending_proposal_responses` → `list_composition_proposals(session_id, status="pending")`.
  Called after the cohort write in send (`routes/messages.py:551`) and recompose (`routes/composer/compose.py:366`).

### 1.4 User row + `message_ingress_receipts`

**Table** `models.py:464-488` (read, full):
```python
message_ingress_receipts_table = Table("message_ingress_receipts", metadata,
    Column("session_id", String, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
    Column("client_request_id", String, primary_key=True),
    Column("user_message_id", String, nullable=False),
    Column("requested_state_id", String, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["user_message_id", "session_id"], ["chat_messages.id", "chat_messages.session_id"],
        name="fk_message_ingress_receipts_user_message_session", ondelete="CASCADE"),
    ForeignKeyConstraint(["requested_state_id", "session_id"], ["composition_states.id", "composition_states.session_id"],
        name="fk_message_ingress_receipts_requested_state_session", ondelete="CASCADE"),
    UniqueConstraint("user_message_id", name="uq_message_ingress_receipts_user_message"),
    *_non_blank_text_constraints("session_id", ...), *_non_blank_text_constraints("client_request_id", ...),
    *_non_blank_text_constraints("user_message_id", ...))
```
`client_request_id` is an **unbounded `String`** (no length CHECK).

**Triggers** (both dialects, read):
- PG: `POSTGRESQL_AUDIT_DDL_COHORT` (`models.py:1453`) entries `trg_message_ingress_receipts_no_update`
  `:1595-1613` (unconditional) and `trg_message_ingress_receipts_no_delete` `:1614-1635`
  (`IF EXISTS (SELECT 1 FROM sessions WHERE id = OLD.session_id)`), installed by the loop `:1889-1899`.
- SQLite: `event.listen` `:1831-1841` (no_update) and `:1843-1855` (no_delete, `WHEN EXISTS (...)`).
- Required set: `schema.py:104-105` in `_REQUIRED_AUDIT_TRIGGERS` (`schema.py:96-110`, **11 names**);
  PG catalogue arm `schema.py:393-396`.

**Writer** `service.py:1396-1423` (read):
```python
def _insert_message_ingress_receipt(self, conn: Connection, /, *, session_id: str, client_request_id: str,
    user_message_id: str, requested_state_id: str | None, created_at: datetime,
    session_operation_context: SessionOperationContext) -> None:
```
COMPOSE-only (`:1411`), asserts the write lock (`:1413`), Family S per-row proof (`:1414`).
Single caller: `add_message_with_transcript._sync` `:4676-4684`.

**Send user-row transaction** `add_message_with_transcript` `service.py:4576-4718` (read): one
`_session_process_locked_begin` + `_session_write_lock` + `_session_composer_mutation_transaction(COMPOSE)`
(`:4627-4637`) → `_existing_message_ingress_result` replay check (`:4638`) → `_assert_state_in_session`
for requested/composition state → `_reserve_sequence_range` → `_insert_chat_message` (user,
`route_user_message`) → `_insert_message_ingress_receipt` → `mark_session_updated` → transcript SELECT on
the same connection. Returns `MessageIngressFresh | MessageIngressAccepted | MessageIngressConflict`
(protocol `:880-900`). **It does not read or compare the current head** (read).

**Every reader** (grep `message_ingress_receipts_table|client_request_id` in `service.py`; module inventory
grep over `src` below):
- `service.py:4508-4537` `_existing_message_ingress_result` (PK lookup; Tier-1 if the row is not a
  `route_user_message` user row; Accepted iff content AND `requested_state_id` equal, else Conflict).
- `service.py:4539-4574` `lookup_message_ingress` (pre-state-preflight probe under the COMPOSE fence).
- `service.py:4739-4776` `get_messages` — LEFT OUTER JOIN on `(user_message_id, session_id)`
  (`:4759-4764`), feeding `_row_to_chat_message_record(..., client_request_id=...)` (`:4720-4737`, Tier-1 if
  attached to a non-user row). Consumer: `routes/_helpers.py:593` puts it on the wire;
  `schemas.py:216` `client_request_id: str | None = None` on the message response.
- Route: `routes/messages.py:111` `_ingress_receipt_conflict`, uses at `:173-182` and `:249-252`.
- Outside sessions (grep, files): `_acceptance_common/replica_probes.py` (7), `_azure_container_apps_acceptance/controller.py` (5),
  `azure_container_apps_acceptance.py` (5), `azure_container_apps_observations.py` (1).
- Recompose: `routes/composer/compose.py` has **0** hits for `client_request_id|message_ingress`
  (positive control `routes/messages.py`: 4).

Tests touching ingress (grep, files): `tests/testcontainer/web/test_add_message_transcript_postgres.py`
(`:59`, `:128` two-instance same-key + cascade), `tests/unit/web/sessions/test_service.py`,
`test_routes.py`, `test_schemas.py`, `test_freeform_route_custody.py`, `test_schema.py`,
`tests/unit/web/acceptance_common/test_replica_probes.py`, ACA acceptance tests, and others (24 files total).

### 1.5 `session_operation_receipts` + events + the hash functions

**Tables** (read): `session_operation_receipts_table` `models.py:724-823` (comment `:724-726`, PK
`(session_id, operation_id)`, `uq_..._request_binding (session_id, operation_id, request_hash)`,
kind CHECK `('session_fork','state_revert')` `:764`, status bundle `:794-802` — **`in_progress` requires
`lease_token` and `lease_expires_at` NOT NULL**, index `ix_session_operation_receipts_status_lease` `:819-823`);
`session_operation_receipt_events_table` `models.py:825-873` (append-only; composite FK to the receipt
incl. `request_hash`, CASCADE). Triggers: `trg_session_operation_receipts_terminal_immutable`
(PG `:1636-1657`, SQLite `:1857-1866`), events `no_update` (`:1658-1676`, `:1868-1876`) and `no_delete`
(`:1677-1698`, `:1878-1887`; `IF EXISTS sessions` delete-guard pattern).

**Codec** `sessions/operation_receipts.py` (read, full file 527 lines):
```python
_REQUEST_SCHEMA = "session-operation-receipt-request.v1"                     # :36
def operation_receipt_request_hash(*, session_id: UUID, kind: OperationReceiptKind, request: BaseModel) -> str:   # :39-53
    config = type(request).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid": raise AuditIntegrityError(...)
    if "operation_id" not in type(request).model_fields: raise AuditIntegrityError(...)
    normalized: dict[str, Any] = request.model_dump(mode="json", exclude={"operation_id"},
        exclude_unset=False, exclude_defaults=False, exclude_none=False)
    return stable_hash({"schema": _REQUEST_SCHEMA, "session_id": str(session_id), "kind": kind, "request": normalized})
def operation_receipt_response_hash(response: BaseModel) -> str:             # :56-62
    # strict+forbid check; re-validate model_dump(mode="python") strict; stable_hash(model_dump(mode="json"))
```
`kind` is not runtime-validated inside the request hash (read); `_validate_identity` `:65-71` is applied
by the table functions, not the hash. Takeover arm: `reserve_operation_receipt` `:272-353` (expired
`in_progress` → new token, `attempt+1`, `taken_over` event). Writers in the module: `_append_event`
`:237`, `reserve_operation_receipt` `:272`, `renew_operation_receipt` `:369`, `bind_operation_receipt`
`:402`, `settle_operation_receipt` `:441` (6 writer sites per the gate pin below).

**Callers** (grep `operation_receipt_request_hash|operation_receipt_response_hash` over `src tests`):
- request hash: `routes/operation_receipts.py:211` (inside `reserve_or_replay_operation_receipt`, def `:193`).
- response hash: `routes/operation_receipts.py:101`, `routes/sessions.py:1031`, `routes/composer/state.py:783`.
- tests: `tests/unit/web/sessions/test_operation_receipts.py:76-82`
  (`test_receipt_request_hash_binds_kind_session_and_payload_but_not_retry_id`), `test_fork.py:1318`,
  comment `test_routes.py:7822`.

**Golden vector: none exists.** `test_operation_receipts.py:74-82` asserts only `first == retried` and
`first != changed` over a local strict `_Request` (`:34-38`: `operation_id: str`, `state_id: str | None`);
grep for a 64-hex literal in that file → 0. Despite its name the test does not vary `kind` or `session_id`.

Table readers outside the module (grep): `service.py` (list_sessions fork filter `:2182-2191`, fork/revert
paths), `coordination/repository.py:723-772` (`decide_and_soft_archive`), `blobs/service.py:487-495,
525-530, 2286-2290`. Receipts reserve under a live SOL: `routes/operation_receipts.py:239-248`
(read) → `service.reserve_operation_receipt` `service.py:1049-1080`.

PG tests: `tests/testcontainer/web/test_operation_receipts_postgres.py:51, :87`;
`test_schema_probe_postgres.py:804` (locators). SQLite: `tests/unit/web/sessions/test_operation_receipts.py`
(`:42, :74, :85, :92, :152, :197`). (`tests/unit/web/sessions/test_operation_receipts_postgres.py`: not found.)

### 1.6 Session-operation lease: acquire / adopt / release

- `repository.py:4646-4733` `_SessionOperationAuthorityRepository.acquire(self, *, session_id: UUID,
  operation_kind: SessionOperationKind, owner_instance_id: str, lease_seconds: int) -> SessionOperationContext`
  (read). The exclusive-kind body is **inline** in one `self._locked_transaction(...)` (`:4675-4723`):
  lock `sessions` row FOR UPDATE, select fence, archived/live/expired-owner checks, epoch+1 CAS on the fence
  row. BLOB_READ goes to `_admit_blob_read` `:4735`. `_locked_transaction` for this class: `:4521`
  (PG subclass override `PostgresSessionOperationRepository._locked_transaction` `:5661`).
- `_advance_exclusive_fence_on_connection` and `start_composer_async_operation`: **not found** (grep over
  `src` → 0; positive control in the same grep family: `def adopt` found in `lifecycle.py`).
- `SessionOperationLease.adopt` exists, `coordination/lifecycle.py:426-504` (read): validates timing,
  shielded `authority.compare_and_swap(context)`, on failure releases via `_raise_adopt_failure_after_release`
  (`:208`). **Zero production callers** (grep `SessionOperationLease.adopt|\.adopt(` over `src`: only
  `SessionOperationLease.adopt_fork_child` at `routes/sessions.py:953`).
- `release` `repository.py:5510` uses Family R exact predicates (`:5534`).
- Precedent for "connection-taking authority helper inside another owner's transaction":
  `settle_operation_receipt(conn, ...)` called from `revert_state_for_operation_receipt` (`service.py:5907`,
  call `:6041`) and `settle_fork_operation_receipt` (`service.py:6551`, call `:6652`).
- PG regression set: `tests/testcontainer/web/test_session_operation_fence_postgres.py` (`:275`, `:322`,
  `:349`, `:372`, `:598`, `:632`, `:677`, …).

### 1.7 `decide_and_soft_archive`

`repository.py:712-808` `_RepositorySessionMutations.decide_and_soft_archive(self, *, archived_at: datetime) -> SessionArchiveDisposition`
(read). Service entry `service.py:2219-2224` (ARCHIVE SOL). Logic:
- any `in_progress` receipt for the session (no kind filter) → unknown kind is Tier-1 `AuditIntegrityError`
  (`:733-734`), known kind → `SessionReceiptInProgressError` (`:735`); incoming active fork likewise (`:736-749`).
- `durable_history_exists` (`:750-800`) includes runs, completion events, fork receipts, approvals,
  reviews, attestations, library entries, **`token_usage_ledger` (`:794-796`) and
  `quota_provider_attempts` (`:797-799`)** → SOFT archive (`:803-808`); otherwise `PHYSICAL_DELETE`.

### 1.8 Epoch 71 and every mirror

- `models.py:59` `SESSION_SCHEMA_EPOCH = 71`. The long per-epoch history comment block is **gone**; only
  `models.py:57-58` remain ("Epoch 71 is the freeform-only session schema…").
- `schema.py:45` `_COORDINATION_HARD_CUT_EPOCH = 71` (comment `:35-44` carries the 68–71 history); exact
  equality enforced in `_validate_coordination_hard_cut_metadata` `schema.py:528-529`.
- Literal mirrors (grep `== 71|epoch 71|"session_epoch": 71|…`, plus cross-line check):
  `tests/unit/contracts/test_web_blob_fencing.py:3205`; `tests/unit/web/sessions/test_interpretation_events_table.py:250`;
  `test_proposal_blob_effect_receipts_schema.py:27`; `test_blob_inline_resolutions_schema.py:73, :75` (PRAGMA);
  `test_schema.py:369`; `CHANGELOG.md:9-10` (cross-line "`SESSION_SCHEMA_EPOCH` advances from 53\nto 71"),
  `:52`, `:90`; `README.md:167-168` (cross-line "session epoch 53\nto 71"); `docs/guides/sharing-pipelines.md:73`;
  `docs/runbooks/staging-session-db-recreation.md:5, 7, 89, 202, 204, 812`;
  `docs/runbooks/aws-ecs-deployment.md:1621, 1649, 1654`; `docs/runbooks/azure-container-apps-deployment.md:109, 451, 464, 538`;
  `docs/runbooks/azure-container-apps-cold-install.md:86`; `website/get-started.html:106`.
- Test files importing `SESSION_SCHEMA_EPOCH` (grep -l, 20 files) include the doc-gate tests
  `tests/unit/docs/test_staging_session_recreation_policy.py`, `test_release_version_surfaces.py`,
  `test_readme_release_surface.py`, `tests/unit/web/test_azure_container_apps_runbook_contract.py`,
  `tests/unit/website/test_release_site_contract.py`.
- The guided epoch pin file is gone: `tests/integration/web/composer/guided/` contains only `__pycache__`.

### 1.9 Trigger inventory (5 places) at this tip

1. `models.py` `POSTGRESQL_AUDIT_DDL_COHORT` `:1453-1699` + SQLite `event.listen` `:1737-1887`.
2. `schema.py:96-110` `_REQUIRED_AUDIT_TRIGGERS` (11).
3. `schema.py:371-406` hand-written PG `relname`/`tgname` arms in `validate_required_triggers` (`:366`).
4. `tests/unit/web/sessions/test_schema.py:603-627` `test_postgres_schema_emits_native_audit_trigger_ddl`
   (list of 11 + `assert "guided_operations" not in ddl`).
5. `tests/testcontainer/web/test_schema_probe_postgres.py:612-645` — `assert names == {…11…}` (exact over all
   session triggers); disable/drop matrix `:779-784`; `:791` stale on missing/disabled.

Partial-index symmetry guard `schema.py:566-622`; precedent index `uq_runs_one_active_per_session`
`models.py:2006`.

### 1.10 Mutation-authority and digest gates (current shape)

`tests/unit/architecture/test_session_db_mutation_authority.py` (19 385 lines):
- `TablePolicy` `:34-44` — `permits` matches `authority` or `(authority, operation)` only; no kind concept.
- Policies: `chat_messages` `:130-138` (SessionMutationAuthority + fork/diagnostics carve-outs);
  `message_ingress_receipts` `:139` (`SessionMutationAuthority`, no carve-outs);
  `session_operation_fences` `:188`; `session_operation_receipt_events` `:189`;
  `session_operation_receipts` `:190-194` (`SessionOperationReceiptAuthority`, `(("SessionForkParentReceiptMutations", {"update"}),)`).
- Receipt symbols bound `:458-468`; pin `test_operation_receipt_writers_are_exact_and_fenced` `:12196-12221`
  (`len(live) == len(reviewed) == 6`, exact tables, exact `operation_authorities`).
- Ingress: symbol `SessionServiceImpl._insert_message_ingress_receipt` `:522`, reviewed writer `:2299-2300`,
  boundary `:19292`, and a **retired-writer pin** `:19314`
  `"SessionServiceImpl.add_message_with_transcript._sync": {"message_ingress_receipts"}` — asserts that
  `_sync` itself writes nothing to ingress; rename probe `:19341-19352`.
- `test_sessions_metadata_table_policy_is_exact_and_protected` `:11267` (live tables == policy tables).
- `test_named_authority_registry_is_explicit_extensible_and_exact` `:11387`.
- `test_all_production_sessions_writers_are_reviewed_typed_authorities` `:18617`: no decorator in the
  preceding lines (read `:18610-18617`), i.e. **un-decorated at this tip; not run in this lane** (old
  findings recorded it XFAIL at `d479eb2b4`).

`tests/unit/architecture/test_digest_column_shape_checks.py`: `_DIGEST_NAME` `:46`, `_INVENTORY` `:72`,
receipts entries `:113-114`. PG arm: `test_schema_probe_postgres.py:182`
`test_postgres_digest_checks_enforce_every_inventoried_shape`.

### 1.11 Request DTOs

`sessions/schemas.py` (read): `_StrictResponse` `:52-63`; `_RequestModel` `:66-69` (`extra="forbid"`, not strict);
`_SessionOperationRequest` `:72-88` (strict+forbid, `operation_id: str` length 36, canonical-UUID validator);
`SendMessageRequest(_RequestModel)` `:143-159` (`content` 1..65536, `state_id: UUID | None = None`,
`client_request_id: UUID` `:154`); `RecomposeRequest(_RequestModel)` `:162-165` (`expected_user_message_id: UUID`).

### 1.12 Head-read and stale-state machinery (ruling 5 inputs)

- Head read idiom: `SELECT composition_states.id WHERE session_id ORDER BY version DESC LIMIT 1`
  (`persist_compose_turn`, `service.py:1780-1795`, raises `StaleComposeStateError` `:1788`).
- `StaleComposeStateError` `protocol.py:1707`; rendered by `app.py:1501-1515` as 409
  `{"error_type":"stale_compose_state","detail":"The session changed while the compose turn was running.","request_id":…}`
  (flat body, handler-injected request id).
- Send today: explicit `state_id` is only a provenance/404 check (`routes/messages.py:190-202`); the compose
  baseline is the actual head (`:205-217`, comment citing elspeth-e08063c3a5: seeding from the client id
  "turned that legitimate lag into an unrecoverable 409 stale_compose_state on every follow-up send").
- Recompose guard: `routes/composer/compose.py:158` `conversation_records[-1].id != body.expected_user_message_id`.

### 1.13 Post-terminal writes a compose turn can make under the same SOL (ruling 2 precondition)

Measured on the synchronous send route (the body the worker will inherit). "Terminal" = the point the
R2 composite would commit: after the success cohort `_persist_turn_audit_cohort` (`routes/messages.py:531`)
and the pending read (`:551`).

| # | Write after the final publication | Anchor | SOL-fenced? | Class |
|---|---|---|---|---|
| P1 | **Auto-title task**: spawned on `compose_operation_lease` when `len(records)==1` and default title (`routes/messages.py:335-357`), joined in the route `finally` for up to 2 s (`:1036-1042`) — i.e. **after** the response is built | `sessions/_auto_title.py:359` | yes | see P1a–P1d |
| P1a | `begin_provider_attempt(source="auto_title")` | `_auto_title.py:402` → `service.py:5481` | Family S | admission (pre-dispatch) |
| P1b | `settle_provider_attempt` / `cancel_undispatched_provider_attempt` | `_auto_title.py:308, :416` → `service.py:5614 / :5545` | Family S | audit/ledger |
| P1c | `update_session_title` → `set_title` (`sessions.title`, `updated_at`) | `_auto_title.py:486` → `service.py:2124/2148` → `repository.py:686` | Family S | **non-audit** |
| P2 | Progress `complete` publish | `routes/messages.py:557` | **no** (0 hits, §1.1) | transport |
| P3 | CRL `finish_request` in `_track_compose_inflight` | `routes/messages.py:142` | **no** | transport |
| P4 | SOL release (fence `released_at`) | `repository.py:5510/5534` | Family R | lease lifecycle |
| P5 | SOL renew heartbeats until close | `repository.py:4954/4986` | Family R | lease lifecycle |

Recompose has no auto-title (grep `auto_title` in `routes/composer/compose.py`: 0; positive control
`routes/messages.py`: hits at `:335-357`). Failure-path persists (`_handle_*`, `_persist_llm_calls` in the
compose `finally`, `routes/messages.py:872-921`, cancelled arm `:963-981`) run **before** the failure
terminal in the worker model, so they are pre-terminal writes; several are non-audit
(`partial_state` → `composition_states` via `_handle_plugin_crash` / `_handle_runtime_preflight_failure`).

### 1.14 Other facts

- `commit_composition_response` (`service.py:5087`) has **no production caller** (grep over `src`: def
  `:5087`, protocol `:3049`, and its own error string `:5122`; one test hit `test_routes.py:698`).
  `commit_transition_response`: 0 hits in `src` (removed with guided).
- `persist_compose_turn` `service.py:1658` (single transaction per intermediate turn; Family S at `:1763`).
- `add_message` `service.py:4352` (`_sync` Family S `:4447`) is the send/recompose assistant writer
  (`routes/messages.py:515`, `compose.py:340`).

---

## 2. DELTA vs old findings (`d479eb2b4`) and `contract.md`

**Moved**
- Epoch `67` → **71** (`models.py:366` → `:59`; `schema.py:40` → `:45`). Contract's "epoch 68"
  (`contract.md:39`, T17) is stale; next cut is **72**.
- `_require_session_operation_context_on_connection` `service.py:4931` → **`:970`**;
  `_session_composer_mutation_transaction` `:4986` → **`:1002`**; `add_messages_atomic` `:14706` → **`:6893`**;
  `_session_process_locked_begin` `:4758` → **`:833`**; `_session_write_lock` `:4800` → **`:875`**.
- DB clock: `_guided_database_now` is gone; `database_now(conn)` from `coordination/database_clock.py`
  (import `mutation_capabilities.py:27`).
- `_REQUIRED_AUDIT_TRIGGERS` `schema.py:97-114` → `:96-110`, now **11** names (3 ingress/receipt families new).
- Composer proposal mutations moved out of `service.py` to `mutation_capabilities.py`
  (`_SessionComposerMutationState` `:57`, `_SessionComposerMutations` `:100`) — commit `6f2ccb32d`.
- Receipts codec: `guided_operations.py:1-44` → `operation_receipts.py:36-62` (same normalisation, now
  `config.get(...)` form, schema tag `session-operation-receipt-request.v1`).

**Vanished** (grep controls in §1)
- `guided_operations`, `guided_operation_events`, `guided_operation_admission_blocks`, the guided trigger,
  `GuidedOperation*` protocol surface, `guided_operation_request_hash`, `_GuidedOperationRequest`,
  `_GuidedSessionMutation*` (dual-fence template), `reserve_guided_operation` (0 hits for
  `guided_operations` and `guided_operation_request_hash|_GuidedOperationRequest` in `src`).
  The contract's `SendMessageRequest(_GuidedOperationRequest)` and the old §4.2 "dual-fence template" no
  longer exist; the only dual-authority template left is the fork one (`service.py:900-963`).
- `SessionGuidedOperationInProgressError` archive refusal → replaced by `SessionReceiptInProgressError`
  over receipts (`repository.py:723-749`).
- `commit_transition_response` (0 hits); `commit_composition_response` is now uncalled.
- The epoch history comment block (`models.py:45-365` old).
- `tests/integration/web/composer/guided/test_schema9_epoch.py` epoch pin.

**New**
- `message_ingress_receipts` (epoch 69, `8630db9b8`) with two triggers, a single writer, a transcript join,
  and a retired-writer pin (`test_session_db_mutation_authority.py:19314`).
- `SendMessageRequest.client_request_id: UUID` (`schemas.py:154`); `RecomposeRequest` already exists with
  `expected_user_message_id` (contract `:161` said "NEW … only operation_id" — wrong now).
- `session_operation_receipts` / events + `SessionOperationReceiptAuthority` + 6-site pin.
- `_require_session_write_authority_on_connection` (`service.py:7165`): **per-row** Family S proof inside
  `_insert_chat_message`, `_insert_message_ingress_receipt`, `_insert_composition_state`.
- Provider-attempt ledger writers (`service.py:5443-5624`, `provider_quota.py:62/149/159`) and
  `add_messages_atomic` charging `token_usage_ledger` in the cohort transaction (`:6995`).
- `decide_and_soft_archive` treats ledger/attempt rows as durable history (`repository.py:794-799`).
- `SessionOperationLease.adopt` (`lifecycle.py:426`) exists — T05's adopt target is real, but has no
  production caller yet.
- `test_all_production_sessions_writers_are_reviewed_typed_authorities` appears un-decorated (was XFAIL).

---

## 3. IMPLICATIONS for the plan under the rulings

**I1 — Where the positive predicate (ruling 2) goes.** It belongs in **Family S only**
(`service.py:970`). It must **not** go into `_exact_active_predicates` (`repository.py:4845`): `renew`,
`release`, `adopt`'s CAS and `archive_delete` all share it, and D12 needs the SOL to renew/release after the
terminal CAS. Consequence: the blob-tool path (`composer/tools/blobs.py:1440, :1683, :1781` via
`compare_and_swap`/`mutate`) is **not** covered by a Family-S-only predicate; it needs its own arm (e.g. a
predicate inside `mutate`'s transaction for COMPOSE contexts, separate from the shared builder) or it is a
hole for cancel-marked turns creating/deleting blobs.

**I2 — `audit_only` must be threaded to the per-row proof.** `add_messages_atomic._write` calls
`_insert_chat_message` (`:6971`) which re-proves at `:1360` regardless of how the transaction was opened.
Minimum threading: `_session_composer_mutation_transaction` (`:1002`), `_require_session_write_authority_on_connection`
(`:7165`), `_insert_chat_message` (`:1289`), and `_SessionComposerMutationState._require_exact`
(`mutation_capabilities.py:83`) if any proposal write is ever audit. The cohort transaction also writes
`token_usage_ledger` (`:6995`) and `sessions.updated_at` (`:6998`) — those ride under the same flag and
must be named as audit-class in T06's inventory.

**I3 — Classifying provider-attempt writers.** `begin_provider_attempt` (`service.py:5443/5481`,
`provider_quota.py:149`) is an **admission** before dispatch: it must stay fenced (not `audit_only`), else
a cancel-marked turn can still dispatch a provider call. `finish_provider_attempt` (`:5515` →
`add_messages_atomic`), `cancel_undispatched_provider_attempt` (`:5528`) and `settle_provider_attempt`
(`:5581`) are audit/ledger settlements and need `audit_only` (they may legitimately land after a cancel
marker). `record_token_usage` (`:5395`) is non-cohort charging for auto-title/run.

**I4 — The post-terminal inventory (ruling 2 precondition) has exactly one non-audit writer today: auto-title.**
P1c (`update_session_title`, a `sessions.title/updated_at` write under the same COMPOSE SOL) runs after the
response is built and is joined only in the route `finally` (`routes/messages.py:1036-1042`). Under the
positive predicate it would be **refused** once the job is terminal. T06/T11 must choose: join auto-title
before the terminal CAS (adds up to its timeout to turn latency), or re-home it (own SOL / audit-class /
drop the join). P1a (begin attempt) after terminal would also be refused — which is correct only if the
auto-title provider call is not started after the terminal. This is also the natural **negative control**
for T06 ("a write under a lingering SOL after the terminal CAS must fail"): drive `update_session_title`
with the same context after the terminal. P2/P3 are not SOL-fenced and are unaffected; P4/P5 are Family R
and must stay unaffected (a second control).

**I5 — Composite terminal building blocks.** Neither half exists as a connection method:
- cohort: extract `add_messages_atomic._write` (`:6945-7000`) into a connection-taking helper (keeping
  `uncheckpointed_envelopes`, ledger charge, `mark_session_updated`), and keep the post-commit telemetry
  projection (`_run_sync_with_post_commit_projection`, `:6805`) on the composite's commit.
- pending read: add a connection variant of `list_composition_proposals` (`:3303-3341`); the per-row
  Tier-1 helpers already take `conn`.
- the assistant writer `add_message._write` (`:4405-4430`) is also a closure.
All three are `SessionMutationAuthority`/composer symbols today; the gate's retired-writer pins
(`test_session_db_mutation_authority.py:19311-19316`) list `add_message._write`,
`add_messages_atomic._write`, `add_message_with_transcript._sync` — moving their SQL changes pinned
symbols, so re-pin in the same commit.

**I6 — Identity (ruling 1) placement and FK order.** The job→ingress agreement lands in the send user-row
transaction `add_message_with_transcript._sync` (`service.py:4627-4697`). Because of the retired-writer pin
(`:19314`) and the TablePolicy narrowing to `ComposerAsyncOperationAuthority`, the job
`user_message_id` UPDATE must be a **connection-taking helper in the compose authority module** (precedent
`settle_operation_receipt(conn, …)` at `service.py:6041/6652`), not inline SQL in `_sync`. With an
ingress→job composite FK `(session_id, client_request_id, user_message_id) → job(session_id, operation_id,
user_message_id)`, the statement order must be **user row → job UPDATE (sets `user_message_id`) → ingress
INSERT**; SQLite FKs are immediate unless declared `DEFERRABLE` (no deferrable FK precedent was checked in
this lane). Ingress `client_request_id` is unbounded `String` today (`models.py:468`); ruling 1 adds a 36-char
bound (CHECK + DTO). The ingress replay/conflict arms (`service.py:4508-4574`, `routes/messages.py:111-126,
173-182, 249-252`) and `lookup_message_ingress` are deleted at cutover; the transcript join
(`service.py:4759-4764`) and `_row_to_chat_message_record` Tier-1 check stay (they read ingress, not the job).
ACA/replica acceptance readers (`replica_probes.py`, ACA controller/facade/observations) must move with
the wire rename.

**I7 — Ruling 5 check placement.** The only transaction that both holds the COMPOSE SOL and precedes the
user row is `add_message_with_transcript._sync`; it currently does not read the head (§1.4). The check
reuses the head-read idiom (`service.py:1780-1795`) inside that transaction before
`_reserve_sequence_range`, raising `StaleComposeStateError` so `app.py:1501-1515` renders today's body.
For recompose the analogous check belongs before the `expected_user_message_id` guard's transcript read
(`compose.py:158`) — recompose writes no user row. Note the send route's own comment
(`routes/messages.py:205-217`, elspeth-e08063c3a5) records that 409-on-lag was previously an
"unrecoverable 409 … on every follow-up send"; the ruling reinstates the refusal with SPA custody as the
recovery (see Q2).

**I8 — Shared normaliser (B′) golden vector.** No byte pin exists today (§1.5). The vector must be computed
from the **unmodified** `operation_receipt_request_hash` / `operation_receipt_response_hash` at this tip and
committed **before** T02 edits `operation_receipts.py:36-62`, for a fixed session UUID, both receipt kinds,
and a strict DTO with defaulted/None fields (to pin materialisation). The extracted function can take
`kind: str` without changing bytes because `kind` enters only as a JSON string. Response-hash callers to
keep byte-identical: `routes/operation_receipts.py:101`, `routes/sessions.py:1031`, `routes/composer/state.py:783`.

**I9 — Composite start (T05).** `acquire`'s exclusive body is inline (`repository.py:4675-4723`) inside
`_locked_transaction` (`:4521`, PG override `:5661`); the extraction target is that block. Regression set:
`test_session_operation_fence_postgres.py:275, 322, 349, 372` plus the receipts SOL acquire path
(`routes/operation_receipts.py:239-248`, tests `test_operation_receipts.py`,
`tests/testcontainer/web/test_operation_receipts_postgres.py`). T05/T11 will be `SessionOperationLease.adopt`'s
first production caller (`lifecycle.py:426`); its CAS is Family R, unaffected by I1.

**I10 — Archive/retention (D7, ruling 4).** Any session whose compose turn dispatched a provider call has
`quota_provider_attempts`/`token_usage_ledger` rows, so `decide_and_soft_archive` soft-archives it
(`repository.py:794-808`); the `sessions` row persists. Therefore the delete guard ("while the session
exists") is effectively permanent for every compose job that reached a provider, and D7's cascade fires only
for sessions that never dispatched one. `decide_and_soft_archive` needs no change for B′ (it reads only
`session_operation_receipts`), consistent with the panel. Delete-guard trigger pattern to copy:
`models.py:1614-1635` (PG) / `:1843-1855` (SQLite).

**I11 — Epoch 72 sweep list** is §1.8 (15 literal sites incl. two cross-line ones) plus the two constants;
trigger additions touch the 5 places in §1.9 (11 → 13 per the panel).

---

## 4. Open questions

1. **Auto-title vs the positive predicate (I4):** join before terminal, move under its own SOL/authority,
   or classify `update_session_title` as audit-class? Owner call; affects T06/T10/T11 and the negative control.
2. **Ruling 5 semantics for `state_id = None`:** "absent means no state" — does an absent `state_id` against an
   existing head refuse with 409, or only an explicit mismatching id? Today absent means "use head for
   provenance" (`routes/messages.py:203-204`). And confirm the owner accepts reinstating the 409 that
   elspeth-e08063c3a5 removed (`routes/messages.py:205-217`).
3. **Blob-tool hole (I1):** is a separate COMPOSE-only predicate inside `repository.mutate` acceptable, given
   `mutate` is shared by blob service/replacement paths for other kinds?
4. **Is `commit_composition_response` dead code** to delete, or the intended freeform primitive for R2's
   state+assistant publication? It has no production caller at this tip.
5. **Ingress FK deferrability (I6):** ship statement ordering (no DEFERRABLE) or declare the ingress→job FK
   deferrable on PG and rely on order on SQLite? Not measured here; needs the T16 PG cascade test either way.
6. **`test_all_production_sessions_writers_are_reviewed_typed_authorities` (`:18617`)**: un-decorated at this
   tip but not run here; T03/T05/T06 should run it before and after to compare, not assume XFAIL.
