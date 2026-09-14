### Task I3: Approvals — request, decide, withdraw, supersede, and the R2 execute gate

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I2. Runs before: I4, I5. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I8 (the `workflow_governance` switch) and I1 (the
`tests/unit/web/coordination/conftest.py` fixtures and the schema_version-2
`AdmissionPolicyEvidence`); I4, I5 and I6 follow it. Spec: R2
(sso-design.md:1060-1063), R8 (:1089-1101), the `approvals` /
`approval_decisions` rows (:1416-1417), §Testing → Workflow governance
(:1276-1310: "R2 through the real binding tuple", "the losing half of the
concurrent-decide guard"). Every `path:line` below was measured on HEAD
072141b75; I0, I8 and I1 add lines above some of them, so re-measure with the
anchor text quoted beside each line before editing.

Decisions this task owns:

1. **`auth_events` is a Landscape table** (`core/landscape/schema.py:2542`;
   `ck_auth_events_event_type` :2566-2578 already admits `approval_requested`
   and `approval_decided`). The sessions-side authority never opens the
   Landscape: `request`, `decide` and `withdraw` take a required `record`
   callback (the `identity_authority.py:15-38` rule) and invoke it after the
   rows are written and before commit; the routes hand in
   `AuthAuditRecorder.record_approval_requested` / `record_approval_decided`.
   `supersede_open` takes NO callback: its callers are the composition-state
   writers below the audit seam (`coordination/repository.py:793`,
   `sessions/service.py:6260`), exactly where `RepositoryApprovalLifecycleAuthority.apply`
   (`approval_lifecycle_authority.py:21`) already writes `revoked` rows with
   no Landscape row of its own. The superseded transition is recorded on the
   sessions row (`decision='superseded'`, `decided_at`). This DEPARTS from
   the earlier working decision, which listed `superseded` among
   `record_approval_decided`'s decisions (approved, rejected, revoked,
   superseded); it needs an operator ruling before execution (thread a
   `record` callback through the composition-state writers, or rule that
   `superseded` is not audited). See open question 1.
2. **The binding.** `build_approval_binding(*, evidence, config_hash,
   canonical_version, openrouter_catalog_sha256, runtime_val_manifest_sha256)`.
   `policy_hash` and `binding_generation_fingerprint` come from
   `WebPluginPolicyEvidence` (`contracts/plugin_policy_audit.py:49-61`);
   `snapshot_hash` is excluded (it embeds the principal scope, models.py:3600
   comment). `canonical_version = CANONICAL_VERSION` (`contracts/hashing.py:28`,
   `"sha256-rfc8785-v1"`). `openrouter_catalog_sha256` is
   `ExecutionServiceImpl._openrouter_catalog_sha256` (execution/service.py:943,
   set from `app.state.openrouter_catalog_sha256`, app.py:746) at request time
   and `_EnvelopePayload.openrouter_catalog_sha256` (envelope.py:121, read
   back by `read_cancelled_execution_envelope` :156) at permit time.
   `runtime_val_manifest_sha256 = sha256(canonical_json(build_runtime_val_manifest()))`
   (`contracts/runtime_val_manifest.py:1751`; the same bytes
   `run_lifecycle_repository.py:226` persists as `runtime_val_manifest_json`)
   after `prepare_for_run()` (`engine/orchestrator/bootstrap.py:30`, idempotent
   — `build_runtime_val_manifest` raises `FrameworkBugError` on unfrozen
   registries, :1715-1725). `config_hash = stable_hash(deep_thaw(audit_safe_config))`
   where `audit_safe_config` is the profile-lowered, guard-annotated config
   `execute_pipeline` builds at execution/service.py:1809 and persists in the
   envelope (`retain_execution_inputs` replaces only `executable_config`,
   retained_inputs.py:116, so the persisted value is the one hashed). It is
   NOT the Landscape `runs.config_hash` (`stable_hash(config.config)`,
   run_lifecycle_repository.py:372, which only exists once the worker thread
   has loaded settings). See open question 2.
3. **Withdraw maps onto `revoked`** with identity provenance
   (`ck_approvals_revocation_provenance`, models.py:3639-3645). No new
   decision value: the CHECK at :3581 is closed and I0 is over.
4. **Lock order** (`approval_lifecycle_authority.py:3-8`):
   the session lock (`locked_session_transaction`, sessions/locking.py:280),
   then — only when the requester holds deployment-wide `admin` — the admin
   population lock (`_ADMIN_HOLDER_ROWS_FOR_UPDATE`, identity_authority.py:797,
   exposed here as `lock_admin_population(conn)`), then both participating
   `identities` rows `FOR UPDATE` in stable identity-id order, then the
   approvals rows. R8 (:1089) bars an admin approver, so only the requester
   side can hold admin.
5. **R2 lives in `_assess`** (run_start_permit_authority.py:37), which
   already locks the run row (:42) and therefore has `runs.state_id`; the
   caller passes `approval: ApprovalGateInputs | None` and
   `None` means the deployment has `workflow_governance="off"`. The HTTP 409
   is raised by a pre-flight in `execute_pipeline` (execution/service.py:1862-1870,
   before `create_run`) through the same `evaluate_approval_gate` function,
   so the user-facing refusal and the durable refusal cannot disagree.
6. **The manifest** (`tests/unit/architecture/test_session_db_mutation_authority.py`)
   already declares `approvals` and `approval_decisions` as `ApprovalAuthority`
   (:99-109). This task binds the new symbols (:262) and reviews every new
   write and re-fingerprinted write (:1198); the gate reports drift as an
   XFAIL whose text lists each site's `fp=` (:18343-18370), and the task is
   done only when that test reports PASSED.

**Files:**
- Create: `src/elspeth/web/coordination/approval_authority.py`
- Create: `src/elspeth/web/sessions/routes/workflow/__init__.py`, `src/elspeth/web/sessions/routes/workflow/approvals.py`
- Modify: `src/elspeth/contracts/chargeable_admission.py:20-27` (`AdmissionRefusalReason`; the last member on HEAD is `POLICY_GENERATION_CHANGED`, after I1 it is `QUOTA_EXCEEDED`) and `:90-99` (`_consistent_decision`'s `expected_disposition` table)
- Modify: `src/elspeth/web/coordination/identity_authority.py:797` (after `_ADMIN_HOLDER_ROWS_FOR_UPDATE`: the public `lock_admin_population` helper)
- Modify: `src/elspeth/web/coordination/run_start_permit_authority.py:36-104` (`_assess` :37-90, `assess` :93, `issue` :101)
- Modify: `src/elspeth/web/coordination/repository.py:793-886` (`_RepositoryCompositionStateMutations.append_state`; INSERT :837, dead-site sweep :879-886), `:1240-1290` (`create_or_reconcile_pending`'s head append; INSERT :1246, sweep after :1282), `:1552-1571` (`_RepositoryRunMutations.assess_start_admission` :1552, `issue_start_permit` :1562)
- Modify: `src/elspeth/web/sessions/protocol.py:3392-3394` (`SessionOperationRunMutations.issue_start_permit` / `assess_start_admission`), `:4678-4682` (`SessionServiceProtocol.issue_run_start_permit` / `assess_run_start_admission`)
- Modify: `src/elspeth/web/sessions/service.py:6260-6427` (`_insert_composition_state`; INSERT :6377, `_supersede_dead_site_pending_interpretation_events` call :6422-6426), `:10055-10073` (`assess_run_start_admission` :10055, `issue_run_start_permit` :10065)
- Modify: `src/elspeth/web/execution/service.py:880-897` (`ExecutionServiceImpl.__init__` keyword list; `approval_authority` goes after `principal_is_active`), `:1597-1870` (`execute_pipeline` from `plugin_snapshot = self._plugin_snapshot_for_user(user_id, operation="execution")` :1597 to `run_record = await self._session_service.create_run(` :1870; `frozen_run_settings = FrozenRunSettings(` :1813, `retain_execution_inputs` :1817, envelope :1862), `:1932` (`recover_run`'s `assess_run_start_admission`), `:2447-2469` (`_run_pipeline`'s assess / issue)
- Modify: `src/elspeth/web/execution/protocol.py:87-160` (`ExecutionService` gains `compile_approval_binding`)
- Modify: `src/elspeth/web/execution/routes.py:1128-1136` (the `except ExecutionSecretApprovalRequired` arm of `execute_pipeline`; the new arm goes directly before it)
- Modify: `src/elspeth/web/auth/audit.py:45` (`AuthAuditWriter` Protocol; last member `record_relationship_changed`), `:268-287` (`AuthAuditOperation`), `:1140-1180` (after `AuthAuditRecorder.record_relationship_changed`)
- Modify: `src/elspeth/web/app.py:584-597` (`execution_service = ExecutionServiceImpl(` inside `lifespan` :509; the `approval_authority=` kwarg goes after `principal_is_active=app.state.principal_is_active,` :597), `:1521-1522` (`identity_authority` wiring inside `create_app` :1132; the approval authority goes after it), `:164` (`from elspeth.web.shareable_reviews.routes import create_shareable_reviews_router`; the approvals-router import goes beside it) and `:1782` (`app.include_router(create_identity_admin_router())`; the approvals router is registered on the next line, which is the line I4 anchors its reviews router after)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:262` (`_NAMED_AUTHORITY_SYMBOLS`), `:1198` (`_REVIEWED_WRITERS`; rows to re-fingerprint: `RepositoryRunStartPermitAuthority._assess` ×3 at :1213-1246, `SessionServiceImpl._insert_composition_state` composition_states insert at :2203-2212, `_RepositoryCompositionStateMutations.append_state` at :2213-2222, `_RepositoryInterpretationMutations.create_or_reconcile_pending` composition_states insert at :2278-2287)
- Modify: `tests/unit/web/sessions/test_operation_fence_wiring.py:476-490` (the facet-signature pin for `assess_start_admission` / `issue_start_permit`)
- Modify: `tests/unit/web/execution/test_service.py:751-776` (the two permit fakes on `mock_session_service`)
- Modify: `tests/unit/web/auth/test_identity_admin_routes.py:50-95` (`_RecordingAuditWriter`, "every `AuthAuditWriter` member, explicit")
- Modify: `tests/unit/web/composer/test_chargeable_admission.py` (append the refusal-reason pins)
- Modify: `tests/unit/web/auth/test_audit.py` (append after `_metadata` :645; helpers `_durable_recorder` :630, `_durable_rows` :634)
- Modify: `CHANGELOG.md:35-38` (the `**Authentication events in signed exports.**` bullet under `## 0.8.1 - 2026-09-10`; the I3 bullet goes directly after it)
- Modify: `config/cicd/soft-mapping-census.yaml` (re-pinned by `scripts.check_contracts --write-census`: `ApprovalBinding.from_json(value: Mapping[str, object])` is one new soft parameter)
- Test (new): `tests/unit/web/coordination/test_approval_authority.py`, `tests/unit/web/coordination/test_r2_execute_gate.py`, `tests/unit/web/coordination/test_state_writers_supersede_approvals.py`, `tests/unit/web/execution/test_approval_gate_wiring.py`, `tests/unit/web/workflow/__init__.py`, `tests/unit/web/workflow/test_approval_routes.py`, `tests/integration/web/workflow/__init__.py`, `tests/integration/web/workflow/test_approvals.py`, `tests/testcontainer/web/test_approval_decide_race_postgres.py`

**Interfaces:**
- Consumes:
  - I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"` (read as `settings.workflow_governance == "on"`, never ANDed with the R11 predicate); fixtures `closed_local_settings(tmp_path)` and `closed_local_app(tmp_path, closed_local_settings) -> SyncASGITestClient` in `tests/unit/web/conftest.py` (`auth_provider="local"`, `registration_mode="closed"`, `workflow_governance="on"`, `compartment_id="test-compartment"`, `get_current_user` overridden to `alice`, `client.app.state.phase3_engine` / `phase3_sessions_service` exposed).
  - I1: `tests/unit/web/coordination/conftest.py::fenced_session` — attributes `engine`, `connection_token` (a registered mutation-connection token whose `engine.begin()` transaction stays open for the test, so `begin_nested()` savepoints work), `identity_id` (an active identity that owns the session), `session_id`. I3 does NOT consume `pg_fenced`: a concurrent-decide race needs two independent transactions on committed rows, which one open fixture token cannot give, so the PostgreSQL loser test builds its own database on `external_deployment_postgres_url` (tests/testcontainer/web/conftest.py:40) the way `tests/testcontainer/web/test_approval_lifecycle_postgres.py:31-79` does. The earlier working decision had I3 reuse I1's fixture names, `pg_fenced` included; this task departs from it for the reason above (see open question 3). I1's `AdmissionPolicyEvidence` (`schema_version: Literal[2]`) and `AdmissionRefusalReason.QUOTA_EXCEEDED` are already present.
  - HEAD (routes and integration): `SessionServiceProtocol.get_current_state(session_id)` (sessions/protocol.py:4613) and `get_state(state_id)` (:4618); `RepositoryIdentityAuthority.holds_active_role(*, identity_id, role)` (identity_authority.py:1285); `SessionOperationLease.acquire(...)` (coordination/lifecycle.py:326), `.context` (:598), `.close()` (:1035); `run_sync_in_worker(func, *args, **kwargs)` (web/async_workers.py:179); `SessionOperationKind.BLOB_READ` / `COMPOSE` (contracts/session_operation.py:10-18); `_save_composition_state_with_compose_authority(service, session_id, state, *, provenance)` (tests/integration/web/conftest.py:86); `LandscapeDB.from_url(...).read_only_connection()` and `auth_events_table` (the read `tests/unit/web/auth/test_audit.py:634-641` uses); `AuthAuditRecorder(landscape_url=, landscape_passphrase=, create_tables=)` (auth/audit.py:431; `close()` :459 makes every later write raise `RuntimeError("Auth audit recorder is closed")` :448-449).
  - HEAD: `_resolve_mutation_connection` / `_register_mutation_connection` / `_unregister_mutation_connection` (mutation_connection_registry.py:22-46); `locked_session_transaction(engine, session_id)` (sessions/locking.py:280); `database_now(conn)` (coordination/database_clock.py:54); `_ADMIN_HOLDER_ROWS_FOR_UPDATE` (identity_authority.py:797); `RepositoryChargeableAdmissionAuthority.assess` (run_start_permit_authority.py:41); `read_cancelled_execution_envelope` (envelope.py:156, returns `CancelledExecutionEnvelope(audit_safe_config, plugin_snapshot, user_id, auth_provider_type, openrouter_catalog_sha256, openrouter_catalog_source, web_plugin_policy_evidence)`); `_build_web_plugin_policy_evidence(snapshot=, policy=)` (execution/service.py:404); `SessionOperationLease.acquire` with `SessionOperationKind.BLOB_READ` (execution/routes.py:955-961); `_verify_session_ownership(session_id, user, request)` (sessions/routes/_helpers.py:2527); `request.app.state.auth_audit_recorder` (identity_admin_routes.py:328); `_admin_provenance(request, *, actor_identity_id, on_behalf_of, console_request_id)` (audit.py:1206); `ensure_test_identity` (tests/fixtures/identities.py:10); `SQLiteLocalSessionOperationAuthority` (coordination/sqlite_authority.py:18) with `create_session_with_initial_fence(user_id=, title=, auth_provider_type=, owner_instance_id=, lease_seconds=)` (repository.py:4538), `acquire(session_id=, operation_kind=, owner_instance_id=, lease_seconds=)` (:4623), `mutate(context, mutation)` (:5121).
- Produces:
  - `src/elspeth/web/coordination/approval_authority.py`:
    - `ApprovalBinding(config_hash: str, canonical_version: str, runtime_val_manifest_sha256: str, openrouter_catalog_sha256: str, binding_generation_fingerprint: str, policy_hash: str)` frozen dataclass; `as_json() -> dict[str, str]`; `ApprovalBinding.from_json(value: Mapping[str, object]) -> ApprovalBinding` (raises `ValueError` on a missing or non-string key).
    - `build_approval_binding(*, evidence: WebPluginPolicyEvidence, config_hash: str, canonical_version: str, openrouter_catalog_sha256: str, runtime_val_manifest_sha256: str) -> ApprovalBinding`
    - `BOUND_EVIDENCE_FIELDS: tuple[str, ...] = ("binding_generation_fingerprint", "policy_hash")` and `EXCLUDED_EVIDENCE_FIELDS: Mapping[str, str]` (field → reason) — the closed decision over `WebPluginPolicyEvidence` fields, pinned by `test_binding_fields_are_a_closed_decision_over_web_plugin_policy_evidence`.
    - `ApprovalGateInputs(evidence: WebPluginPolicyEvidence, config_hash: str, canonical_version: str, openrouter_catalog_sha256: str, runtime_val_manifest_sha256: str)` frozen dataclass with `binding` property.
    - `runtime_val_manifest_sha256() -> str` (calls `prepare_for_run()` then hashes the manifest).
    - `evaluate_approval_gate(*, approved: ApprovalBinding | None, compiled: ApprovalBinding) -> AdmissionRefusalReason | None` (`None` → `APPROVAL_REQUIRED`; any field differs → `APPROVAL_BINDING_MISMATCH`; equal → `None`).
    - `ApprovalRecord(approval_id, session_id, state_id, binding: ApprovalBinding, requested_by_identity_id, approver_identity_id, requested_at, decided_at, decision: Literal["approved","rejected","revoked","superseded"] | None, request_note, decision_seen_at, decided_by_identity_id, decision_note, revoked_by_identity_id, revocation_actor_kind, revocation_event_id)` frozen dataclass.
    - Refusals (all subclass `ApprovalRefusal(RuntimeError)`): `ApprovalNotFound`, `ApprovalAlreadyDecided(current_state: str)`, `ApprovalAuthorIsApprover`, `ApproverRoleRequired`, `ApprovalParticipantNotActive(identity_id)`, `ApprovalOpenRequestExists`, `ApprovalNoteRequired`, `ApprovalNoteTooLong`, `ApprovalWithdrawRequiresRequester`.
    - `RepositoryApprovalAuthority` (static, token-based, runs inside the caller's transaction):
      - `request(connection_token, *, session_id: str, state_id: str, binding: ApprovalBinding, requested_by: str, approver: str, note: str | None, now: datetime, record: Callable[[ApprovalRecord], None]) -> ApprovalRecord`
      - `decide(connection_token, *, approval_id: str, decided_by: str, decision: Literal["approved", "rejected"], note: str | None, now: datetime, record: Callable[[ApprovalRecord], None]) -> ApprovalRecord`
      - `withdraw(connection_token, *, approval_id: str, requested_by: str, now: datetime, record: Callable[[ApprovalRecord], None]) -> ApprovalRecord` — a superset of the earlier working signature `withdraw(connection_token, *, approval_id, requested_by)`: `now` and `record` follow the `request`/`decide` shape because decision 1 above requires every audited authority mutation to take a required `record` callback (see open question 3).
      - `supersede_open(connection_token, *, session_id: str, now: datetime) -> tuple[str, ...]` (the superseded approval ids)
      - `approved_binding(connection_token, *, session_id: str, state_id: str) -> ApprovalBinding | None` (locks the row `FOR UPDATE`)
      - `read(connection_token, *, approval_id: str) -> ApprovalRecord`
    - `supersede_open_approvals(connection: Connection, *, session_id: str, now: datetime) -> tuple[str, ...]` — the ONLY statement that writes `decision='superseded'`; the token form above delegates to it; the four composition-state head writers call it on their own connection, mirroring `supersede_dead_site_pending_interpretation_events` (dead_site_supersession.py:55).
    - `ApprovalTransactionAuthority(engine)`: `run[T](session_id: str, mutation: Callable[[str, datetime], T]) -> T` (opens `locked_session_transaction`, reads `database_now`, mints a token, calls `mutation(token, now)`, unregisters); reads `session_id_of(approval_id) -> str | None`, `approved_binding(*, session_id, state_id) -> ApprovalBinding | None`, `inbox(*, approver_identity_id) -> tuple[ApprovalRecord, ...]`, `sent(*, requested_by_identity_id) -> tuple[ApprovalRecord, ...]`. Mounted as `app.state.approval_authority`.
  - `src/elspeth/contracts/chargeable_admission.py`: `AdmissionRefusalReason.APPROVAL_REQUIRED = "approval_required"`, `AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"`, both mapped to `QuotaDisposition.NOT_ASSESSED`.
  - `src/elspeth/web/coordination/identity_authority.py`: `lock_admin_population(conn: Connection) -> None`.
  - Widened permit path: `RepositoryRunStartPermitAuthority.assess(connection_token, *, run_id, context, now, policy, approval: ApprovalGateInputs | None)` and `issue(...)` (same widening); `SessionOperationRunMutations.assess_start_admission(*, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None)` and `issue_start_permit(...)`; `SessionServiceProtocol.assess_run_start_admission(run_id, *, session_operation_context, approval: ApprovalGateInputs | None = None)` and `issue_run_start_permit(...)`.
  - `src/elspeth/web/execution/service.py`: `ExecutionServiceImpl.compile_approval_binding(session_id: UUID, state_id: UUID, *, user_id: str, session_operation_context: SessionOperationContext) -> ApprovalBinding` (also on the `ExecutionService` Protocol); `ExecutionApprovalRequired(reason: AdmissionRefusalReason, binding: ApprovalBinding)` raised by `execute_pipeline` before `create_run`; the route maps it to HTTP 409 with `detail = {"error_type": "approval_required" | "approval_binding_mismatch", "detail": "Run admission refused: <error_type>", "binding": ApprovalBinding.as_json()}` (the six keys).
  - `src/elspeth/web/auth/audit.py`: `AuthAuditWriter.record_approval_requested(request: Request | None, *, provider: AuthProviderType, approval: ApprovalRecord) -> None` and `record_approval_decided(request: Request | None, *, provider: AuthProviderType, approval: ApprovalRecord, actor_identity_id: str) -> None`; `AuthAuditOperation.APPROVAL_REQUESTED` / `APPROVAL_DECIDED`. Rows: `event_type="approval_requested"`, `identity_id=requested_by_identity_id`, metadata `{actor, on_behalf_of: None, console_request_id: None, method, path, approval_id, session_id, state_id, approver_identity_id, binding, note}`; `event_type="approval_decided"`, `identity_id=actor_identity_id`, metadata adds `decision` (`approved` | `rejected` | `revoked`), `actor_kind="identity"`, `requested_by_identity_id`, `decided_by_identity_id`, `note`. Notes are `_bounded_text` (512) in metadata; the 4 KiB original is the sessions row.
  - Routes (byte-exact; I9 consumes): `POST /api/sessions/{session_id}/approvals` (201), `GET /api/approvals/inbox`, `GET /api/approvals/sent`, `POST /api/approvals/{approval_id}/decide`, `POST /api/approvals/{approval_id}/withdraw`. Router package `src/elspeth/web/sessions/routes/workflow/`: `__init__.py` is a docstring-only package marker (I4 and I5 add nothing to it); `approvals.py` exposes `create_approvals_router() -> APIRouter` (`tags=["workflow-approvals"]`, full paths, no prefix), registered in `web/app.py` by one `app.include_router(create_approvals_router())` line directly after `app.include_router(create_identity_admin_router())` (:1782). I4 (`reviews.py`, `create_reviews_router()`) and I5 (`library.py`, `create_library_router()`) each add their own module and `include_router` line — the shape those written blocks already use. Refusal envelope: `{"error_type": <code>, "detail": <str>}` with 409 for `workflow_governance_off` (every mutation while `workflow_governance != "on"`; the inbox and sent lists are empty then), `approval_already_decided` (+ `"current_state"`), `approval_author_is_approver`, `approver_role_required`, `approval_participant_not_active`, `approval_open_request_exists`, `approval_note_required`, `approval_note_too_long`, `approval_withdraw_requires_requester`; 404 `approval_not_found`, `state_not_found`, and the plain `Session not found` from `_verify_session_ownership`. Wire shapes `ApprovalRequestBody(state_id: UUID | None, approver_identity_id: str, note: str | None)`, `ApprovalDecisionBody(decision: Literal["approved","rejected"], note: str | None)`, `ApprovalView` (every `ApprovalRecord` field, `binding` as `dict[str, str]`), `ApprovalListResponse(approvals: list[ApprovalView])`.

- [ ] **Step 1: Write the failing contract tests for the two refusal reasons.**

Append to `tests/unit/web/composer/test_chargeable_admission.py`:

```python
def test_approval_refusals_are_closed_members_with_no_quota_assessment() -> None:
    """R2 refuses inside the permit path, so its two reasons are admission refusals (sso-design.md:1060)."""
    from elspeth.contracts.chargeable_admission import (
        AdmissionPolicyEvidence,
        AdmissionRefusalReason,
        ChargeableAdmissionDecision,
        QuotaDisposition,
    )

    assert AdmissionRefusalReason.APPROVAL_REQUIRED.value == "approval_required"
    assert AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH.value == "approval_binding_mismatch"
    for reason in (AdmissionRefusalReason.APPROVAL_REQUIRED, AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH):
        decision = ChargeableAdmissionDecision(
            evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash="a" * 64),
            refusal_reason=reason,
        )
        assert decision.allowed is False
        assert decision.canonical_hash == ChargeableAdmissionDecision.model_validate_json(decision.model_dump_json()).canonical_hash


def test_approval_refusals_reject_an_assessed_quota_disposition() -> None:
    """Mutation-derivation: the disposition table, not the enum, is what admits the pair."""
    import pytest

    from elspeth.contracts.chargeable_admission import (
        AdmissionPolicyEvidence,
        AdmissionRefusalReason,
        ChargeableAdmissionDecision,
        QuotaDisposition,
    )

    with pytest.raises(ValueError, match="Admission refusal contradicts quota assessment"):
        ChargeableAdmissionDecision(
            evidence=AdmissionPolicyEvidence(
                quota_disposition=QuotaDisposition.POLICY_MISSING, secret_wiring_hash="a" * 64
            ),
            refusal_reason=AdmissionRefusalReason.APPROVAL_REQUIRED,
        )
```

- [ ] **Step 2: Run the contract tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/composer/test_chargeable_admission.py -n 0 -k approval_refusals > /tmp/i3-lane-contract-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; both tests fail with `AttributeError: APPROVAL_REQUIRED` (the enum has no such member).

- [ ] **Step 3: Add the two members and their disposition rows.**

In `src/elspeth/contracts/chargeable_admission.py`, after the last member of `AdmissionRefusalReason` (`:26` on HEAD, `QUOTA_EXCEEDED` after I1):

```python
    # R2 (sso-design.md:1060): refused inside the permit path, so they are
    # admission refusals. Neither assesses a quota, hence NOT_ASSESSED below.
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"
```

In `_consistent_decision`'s `expected_disposition` dict (`:90-99` on HEAD, directly after the `POLICY_GENERATION_CHANGED: QuotaDisposition.NOT_ASSESSED,` row):

```python
                AdmissionRefusalReason.APPROVAL_REQUIRED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH: QuotaDisposition.NOT_ASSESSED,
```

- [ ] **Step 4: Run the contract suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/coordination/test_cli_quota_admission.py -n 0 > /tmp/i3-lane-contract-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 5: Write the failing audit-writer tests.**

Append to `tests/unit/web/auth/test_audit.py` (after `_metadata` at :645; `_durable_recorder` :630 and `_durable_rows` :634 are the helpers):

```python
def _approval_record(**overrides: Any) -> Any:
    from datetime import UTC, datetime

    from elspeth.web.coordination.approval_authority import ApprovalBinding, ApprovalRecord

    values: dict[str, Any] = {
        "approval_id": "approval-1",
        "session_id": "session-1",
        "state_id": "state-1",
        "binding": ApprovalBinding(
            config_hash="1" * 64,
            canonical_version="sha256-rfc8785-v1",
            runtime_val_manifest_sha256="2" * 64,
            openrouter_catalog_sha256="3" * 64,
            binding_generation_fingerprint="4" * 64,
            policy_hash="5" * 64,
        ),
        "requested_by_identity_id": "author",
        "approver_identity_id": "approver",
        "requested_at": datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
        "decided_at": None,
        "decision": None,
        "request_note": "please approve, it's for entirely legitimate business",
        "decision_seen_at": None,
        "decided_by_identity_id": None,
        "decision_note": None,
        "revoked_by_identity_id": None,
        "revocation_actor_kind": None,
        "revocation_event_id": None,
    }
    values.update(overrides)
    return ApprovalRecord(**values)


def test_approval_requested_row_is_anchored_on_the_requester_and_carries_the_binding(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_approval_requested(_request(), provider="local", approval=_approval_record())
    (row,) = _durable_rows(url)
    assert row.event_type == "approval_requested"
    assert row.outcome == "success"
    assert row.identity_id == "author"
    metadata = _metadata(row)
    assert metadata["actor"] == "author"
    assert metadata["approval_id"] == "approval-1"
    assert metadata["session_id"] == "session-1"
    assert metadata["state_id"] == "state-1"
    assert metadata["approver_identity_id"] == "approver"
    assert metadata["binding"]["binding_generation_fingerprint"] == "4" * 64
    assert set(metadata["binding"]) == {
        "config_hash",
        "canonical_version",
        "runtime_val_manifest_sha256",
        "openrouter_catalog_sha256",
        "binding_generation_fingerprint",
        "policy_hash",
    }
    assert metadata["note"] == "please approve, it's for entirely legitimate business"
    assert metadata["method"] == "POST"
    assert set(_PROVENANCE) <= set(metadata)


@pytest.mark.parametrize(
    ("decision", "actor", "expected_actor"),
    [("approved", "approver", "approver"), ("rejected", "approver", "approver"), ("revoked", "author", "author")],
)
def test_approval_decided_row_is_anchored_on_the_deciding_actor(tmp_path: Any, decision: str, actor: str, expected_actor: str) -> None:
    from datetime import UTC, datetime

    recorder, url = _durable_recorder(tmp_path)
    record = _approval_record(
        decision=decision,
        decided_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
        decided_by_identity_id="approver" if decision != "revoked" else None,
        decision_note="because" if decision == "rejected" else None,
        revoked_by_identity_id="author" if decision == "revoked" else None,
        revocation_actor_kind="identity" if decision == "revoked" else None,
        revocation_event_id="event-1" if decision == "revoked" else None,
    )
    recorder.record_approval_decided(None, provider="local", approval=record, actor_identity_id=actor)
    (row,) = _durable_rows(url)
    assert row.event_type == "approval_decided"
    assert row.identity_id == expected_actor
    assert row.request_id is None
    metadata = _metadata(row)
    assert metadata["decision"] == decision
    assert metadata["actor_kind"] == "identity"
    assert metadata["actor"] == expected_actor
    assert metadata["requested_by_identity_id"] == "author"
    assert metadata["decided_by_identity_id"] == record.decided_by_identity_id
    assert metadata["note"] == record.decision_note


def test_approval_decided_refuses_a_decision_outside_the_identity_made_set(tmp_path: Any) -> None:
    """``superseded`` is a sessions-store transition with no Landscape row (Task I3 decision 1)."""
    recorder, _url = _durable_recorder(tmp_path)
    with pytest.raises(ValueError, match="superseded"):
        recorder.record_approval_decided(None, provider="local", approval=_approval_record(decision="superseded"), actor_identity_id="author")


def test_approval_note_is_bounded_in_metadata(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_approval_requested(None, provider="local", approval=_approval_record(request_note="n" * 4096))
    (row,) = _durable_rows(url)
    assert len(_metadata(row)["note"]) == 512
```

- [ ] **Step 6: Run the audit tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py -n 0 -k approval > /tmp/i3-lane-audit-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; every test errors with `ModuleNotFoundError: No module named 'elspeth.web.coordination.approval_authority'`.

- [ ] **Step 7: Add the two writers to the Protocol, the enum and the recorder.**

In `src/elspeth/web/auth/audit.py`:

1. Under `if TYPE_CHECKING:` (:35-36) add `from elspeth.web.coordination.approval_authority import ApprovalRecord`.
2. In `class AuthAuditWriter(Protocol)` (:45), after `record_relationship_changed`'s signature, add:

```python
    def record_approval_requested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        approval: ApprovalRecord,
    ) -> None: ...

    def record_approval_decided(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        approval: ApprovalRecord,
        actor_identity_id: str,
    ) -> None: ...
```

3. In `class AuthAuditOperation(StrEnum)` (:268-287), after `RELATIONSHIP_CHANGED = "relationship_changed"`:

```python
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
```

4. In `class AuthAuditRecorder`, directly after `record_relationship_changed` (its body ends at :1180 with the closing `)` of `record_auth_event`):

```python
    def record_approval_requested(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        approval: ApprovalRecord,
    ) -> None:
        """Anchored on the requester; the approver and the six-key binding are metadata (sso-design.md:1416)."""
        provenance = _admin_provenance(
            request, actor_identity_id=approval.requested_by_identity_id, on_behalf_of=None, console_request_id=None
        )
        with self._open_landscape(AuthAuditOperation.APPROVAL_REQUESTED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="approval_requested",
                outcome="success",
                provider=provider,
                identity_id=approval.requested_by_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "approval_id": approval.approval_id,
                    "session_id": approval.session_id,
                    "state_id": approval.state_id,
                    "approver_identity_id": approval.approver_identity_id,
                    "binding": approval.binding.as_json(),
                    "note": _bounded_text(approval.request_note),
                },
                **provenance.request_columns,
            )

    def record_approval_decided(
        self,
        request: Request | None,
        *,
        provider: AuthProviderType,
        approval: ApprovalRecord,
        actor_identity_id: str,
    ) -> None:
        """One row per identity-made decision: approved, rejected, or the requester's own withdrawal (``revoked``).

        ``superseded`` is written by the composition-state head writers below
        the audit seam and has no Landscape row (Task I3 decision 1), so it
        is refused here rather than silently accepted.
        """
        if approval.decision not in ("approved", "rejected", "revoked"):
            raise ValueError(f"approval_decided records an identity-made decision, not {approval.decision!r}")
        provenance = _admin_provenance(request, actor_identity_id=actor_identity_id, on_behalf_of=None, console_request_id=None)
        with self._open_landscape(AuthAuditOperation.APPROVAL_DECIDED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="approval_decided",
                outcome="success",
                provider=provider,
                identity_id=actor_identity_id,
                user_id=None,
                username=None,
                failure_category=None,
                metadata={
                    **provenance.metadata,
                    "approval_id": approval.approval_id,
                    "session_id": approval.session_id,
                    "state_id": approval.state_id,
                    "decision": approval.decision,
                    "actor_kind": "identity",
                    "requested_by_identity_id": approval.requested_by_identity_id,
                    "decided_by_identity_id": approval.decided_by_identity_id,
                    "note": _bounded_text(approval.decision_note),
                },
                **provenance.request_columns,
            )
```

5. In `tests/unit/web/auth/test_identity_admin_routes.py`, in `_RecordingAuditWriter` after `record_relationship_changed` (:90-91):

```python
    def record_approval_requested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_approval_requested", request, kwargs)

    def record_approval_decided(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_approval_decided", request, kwargs)
```

The audit tests still fail after this step because `approval_authority.py` does not exist yet; Step 12 is where they go green. Continue.

- [ ] **Step 8: Write the failing authority tests.**

Create `tests/unit/web/coordination/test_approval_authority.py`:

```python
"""``RepositoryApprovalAuthority`` against a real sessions engine, through the I1 ``fenced_session`` token.

Every numbered refusal has a fire test and a mutation-derivation test: the
authority row is changed (a role revoked, a note supplied, a binding field
flipped), never the guard.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination import approval_authority as module
from elspeth.web.coordination.approval_authority import (
    BOUND_EVIDENCE_FIELDS,
    EXCLUDED_EVIDENCE_FIELDS,
    ApprovalAlreadyDecided,
    ApprovalAuthorIsApprover,
    ApprovalBinding,
    ApprovalNoteRequired,
    ApprovalNoteTooLong,
    ApprovalOpenRequestExists,
    ApprovalParticipantNotActive,
    ApproverRoleRequired,
    ApprovalWithdrawRequiresRequester,
    RepositoryApprovalAuthority,
    build_approval_binding,
    evaluate_approval_gate,
)
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import approval_decisions_table, approvals_table, identities_table, identity_roles_table

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _evidence(**overrides: Any) -> WebPluginPolicyEvidence:
    base = WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="a" * 64,
        snapshot_hash="b" * 64,
        authorized_plugin_ids=("sink:csv", "source:csv"),
        available_plugin_ids=("sink:csv", "source:csv"),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(),
        binding_generation_fingerprint="c" * 64,
        decision_codes=("policy_allowed",),
    )
    return dataclasses.replace(base, **overrides)


def _binding(**overrides: str) -> ApprovalBinding:
    binding = build_approval_binding(
        evidence=_evidence(),
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )
    return dataclasses.replace(binding, **overrides)


def _seed_approver(fenced_session: Any, identity_id: str = "approver", *, role: str = "approver", revoked: bool = False) -> None:
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    ensure_test_identity(conn, identity_id=identity_id)
    conn.execute(
        insert(identity_roles_table).values(
            role_id=f"{identity_id}-{role}",
            identity_id=identity_id,
            role=role,
            granted_at=NOW - timedelta(days=1),
            granted_by_identity_id=identity_id,
            revoked_at=NOW if revoked else None,
        )
    )


def _ignore(_record: Any) -> None:
    return None


def _request(fenced_session: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "session_id": fenced_session.session_id,
        "state_id": "state-1",
        "binding": _binding(),
        "requested_by": fenced_session.identity_id,
        "approver": "approver",
        "note": "please approve",
        "now": NOW,
        "record": _ignore,
    }
    values.update(overrides)
    return RepositoryApprovalAuthority.request(fenced_session.connection_token, **values)


# ── the binding is a closed decision over the evidence ────────────────────────


def test_binding_fields_are_a_closed_decision_over_web_plugin_policy_evidence() -> None:
    """Spec :1416 'Cost is a test': a new evidence field fails here until it is placed in BOUND or EXCLUDED."""
    names = tuple(f.name for f in dataclasses.fields(WebPluginPolicyEvidence))
    assert names == (
        "schema_version",
        "policy_hash",
        "snapshot_hash",
        "authorized_plugin_ids",
        "available_plugin_ids",
        "control_modes",
        "selected_implementations",
        "selected_profile_aliases",
        "plugin_code_identities",
        "binding_generation_fingerprint",
        "decision_codes",
        "admission_decision",
    )
    assert set(BOUND_EVIDENCE_FIELDS) | set(EXCLUDED_EVIDENCE_FIELDS) == set(names)
    assert not set(BOUND_EVIDENCE_FIELDS) & set(EXCLUDED_EVIDENCE_FIELDS)
    assert BOUND_EVIDENCE_FIELDS == ("binding_generation_fingerprint", "policy_hash")
    assert "snapshot_hash" in EXCLUDED_EVIDENCE_FIELDS
    assert "principal" in EXCLUDED_EVIDENCE_FIELDS["snapshot_hash"]


def test_build_approval_binding_reads_exactly_the_bound_fields() -> None:
    binding = _binding()
    assert binding == ApprovalBinding(
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        runtime_val_manifest_sha256="3" * 64,
        openrouter_catalog_sha256="2" * 64,
        binding_generation_fingerprint="c" * 64,
        policy_hash="a" * 64,
    )
    changed = build_approval_binding(
        evidence=_evidence(snapshot_hash="f" * 64),
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )
    assert changed == binding, "snapshot_hash is excluded: changing it must not change the binding"
    assert ApprovalBinding.from_json(binding.as_json()) == binding
    with pytest.raises(ValueError, match="policy_hash"):
        ApprovalBinding.from_json({k: v for k, v in binding.as_json().items() if k != "policy_hash"})


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(ApprovalBinding)])
def test_r2_derives_from_every_binding_field(field: str) -> None:
    """Mutation-derivation: flip ONE field of the approved row and the gate refuses; restore it and the gate admits."""
    compiled = _binding()
    assert evaluate_approval_gate(approved=None, compiled=compiled) is AdmissionRefusalReason.APPROVAL_REQUIRED
    assert evaluate_approval_gate(approved=compiled, compiled=compiled) is None
    flipped = dataclasses.replace(compiled, **{field: "9" * 64})
    assert evaluate_approval_gate(approved=flipped, compiled=compiled) is AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH


# ── request ───────────────────────────────────────────────────────────────────


def test_request_records_note_binding_and_writes_the_audit_callback_before_returning(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    seen: list[Any] = []
    record = _request(fenced_session, record=seen.append)
    assert record.decision is None
    assert record.request_note == "please approve"
    assert record.binding == _binding()
    assert record.requested_at == NOW
    assert seen == [record]
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    row = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == record.approval_id)).one()
    assert row.binding_json == _binding().as_json()
    assert row.required_count == 1


def test_request_refuses_a_second_open_request_for_the_same_state(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    _request(fenced_session)
    with pytest.raises(ApprovalOpenRequestExists, match="state-1"):
        _request(fenced_session)


def test_request_admits_a_second_open_request_once_the_first_is_decided(fenced_session: Any) -> None:
    """Mutation-derivation for the partial unique index: it is predicated on decision IS NULL."""
    _seed_approver(fenced_session)
    first = _request(fenced_session)
    RepositoryApprovalAuthority.decide(
        fenced_session.connection_token, approval_id=first.approval_id, decided_by="approver", decision="approved", note=None, now=NOW, record=_ignore
    )
    second = _request(fenced_session)
    assert second.approval_id != first.approval_id


def test_author_cannot_be_approver(fenced_session: Any) -> None:
    _seed_approver(fenced_session, fenced_session.identity_id)
    with pytest.raises(ApprovalAuthorIsApprover, match=fenced_session.identity_id):
        _request(fenced_session, approver=fenced_session.identity_id)


def test_request_requires_an_active_approver_role(fenced_session: Any) -> None:
    _seed_approver(fenced_session, role="reviewer")
    with pytest.raises(ApproverRoleRequired, match="approver"):
        _request(fenced_session)


def test_request_role_check_derives_from_the_role_row(fenced_session: Any) -> None:
    """Mutation-derivation: the same identity with the role revoked is refused; with it live, admitted."""
    _seed_approver(fenced_session, revoked=True)
    with pytest.raises(ApproverRoleRequired, match="approver"):
        _request(fenced_session)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(update(identity_roles_table).where(identity_roles_table.c.identity_id == "approver").values(revoked_at=None))
    assert _request(fenced_session).approver_identity_id == "approver"


def test_request_refuses_a_non_active_participant(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(update(identities_table).where(identities_table.c.identity_id == "approver").values(access_state="disabled"))
    with pytest.raises(ApprovalParticipantNotActive, match="approver"):
        _request(fenced_session)


def test_request_bounds_the_note_at_4096_bytes(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    with pytest.raises(ApprovalNoteTooLong, match="4096"):
        _request(fenced_session, note="é" * 2049)
    assert _request(fenced_session, note="é" * 2048).request_note == "é" * 2048


def test_request_by_an_admin_requester_takes_the_population_lock_first(fenced_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """approval_lifecycle_authority.py:3-8: population lock, then the identity rows, in that order."""
    _seed_approver(fenced_session)
    _seed_approver(fenced_session, fenced_session.identity_id, role="admin")
    order: list[str] = []
    real_lock = module.lock_admin_population
    real_identity_lock = module._lock_identity_row

    def spy_population(conn: Any) -> None:
        order.append("population")
        real_lock(conn)

    def spy_identity(conn: Any, identity_id: str) -> Any:
        order.append(f"identity:{identity_id}")
        return real_identity_lock(conn, identity_id)

    monkeypatch.setattr(module, "lock_admin_population", spy_population)
    monkeypatch.setattr(module, "_lock_identity_row", spy_identity)
    _request(fenced_session)
    assert order[0] == "population"
    assert order[1:] == [f"identity:{i}" for i in sorted((fenced_session.identity_id, "approver"))]


def test_request_by_a_non_admin_requester_skips_the_population_lock(fenced_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_approver(fenced_session)
    order: list[str] = []
    monkeypatch.setattr(module, "lock_admin_population", lambda conn: order.append("population"))
    _request(fenced_session)
    assert order == []


# ── decide ────────────────────────────────────────────────────────────────────


def _decide(fenced_session: Any, approval_id: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "approval_id": approval_id,
        "decided_by": "approver",
        "decision": "approved",
        "note": None,
        "now": NOW + timedelta(minutes=1),
        "record": _ignore,
    }
    values.update(overrides)
    return RepositoryApprovalAuthority.decide(fenced_session.connection_token, **values)


def test_decide_writes_the_decision_row_and_the_approval_in_one_call(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    seen: list[Any] = []
    open_record = _request(fenced_session)
    decided = _decide(fenced_session, open_record.approval_id, note="looks fine", record=seen.append)
    assert decided.decision == "approved"
    assert decided.decided_by_identity_id == "approver"
    assert decided.decision_note == "looks fine"
    assert decided.decided_at == NOW + timedelta(minutes=1)
    assert seen == [decided]
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    rows = conn.execute(select(approval_decisions_table).where(approval_decisions_table.c.approval_id == open_record.approval_id)).all()
    assert [(r.decided_by_identity_id, r.decision, r.note) for r in rows] == [("approver", "approved", "looks fine")]


def test_any_active_approver_who_is_not_the_author_may_decide(fenced_session: Any) -> None:
    """Spec :1416: eligibility is role-based; ``approver_identity_id`` is the picker's suggestion, not a lock."""
    _seed_approver(fenced_session)
    _seed_approver(fenced_session, "leave-cover")
    open_record = _request(fenced_session)
    decided = _decide(fenced_session, open_record.approval_id, decided_by="leave-cover")
    assert decided.decided_by_identity_id == "leave-cover"
    assert decided.approver_identity_id == "approver"


def test_decide_requires_active_approver_role(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    _seed_approver(fenced_session, "bystander", role="user")
    open_record = _request(fenced_session)
    with pytest.raises(ApproverRoleRequired, match="bystander"):
        _decide(fenced_session, open_record.approval_id, decided_by="bystander")


def test_decide_refuses_the_author_even_with_an_approver_role(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    _seed_approver(fenced_session, fenced_session.identity_id)
    open_record = _request(fenced_session)
    with pytest.raises(ApprovalAuthorIsApprover, match=fenced_session.identity_id):
        _decide(fenced_session, open_record.approval_id, decided_by=fenced_session.identity_id)


def test_reject_requires_a_nonblank_note(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    open_record = _request(fenced_session)
    with pytest.raises(ApprovalNoteRequired, match="rejected"):
        _decide(fenced_session, open_record.approval_id, decision="rejected", note="   ")
    rejected = _decide(fenced_session, open_record.approval_id, decision="rejected", note="wrong sink")
    assert rejected.decision == "rejected"


def test_second_decision_gets_the_current_state_not_a_second_row(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    _seed_approver(fenced_session, "leave-cover")
    open_record = _request(fenced_session)
    _decide(fenced_session, open_record.approval_id, decision="rejected", note="no")
    with pytest.raises(ApprovalAlreadyDecided, match="rejected") as excinfo:
        _decide(fenced_session, open_record.approval_id, decided_by="leave-cover")
    assert excinfo.value.current_state == "rejected"
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    assert len(conn.execute(select(approval_decisions_table)).all()) == 1


@pytest.mark.parametrize("state", ["superseded", "revoked"])
def test_decide_guards_on_decision_not_on_decided_at(fenced_session: Any, state: str) -> None:
    """Spec :1416: superseded/revoked set ``decision`` without a stated ``decided_at``; the guard must still hold."""
    _seed_approver(fenced_session)
    open_record = _request(fenced_session)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    values: dict[str, Any] = {"decision": state, "decided_at": None}
    if state == "revoked":
        values.update(decided_at=NOW, revocation_actor_kind="system", revocation_event_id="event-x")
    conn.execute(update(approvals_table).where(approvals_table.c.approval_id == open_record.approval_id).values(**values))
    with pytest.raises(ApprovalAlreadyDecided, match=state):
        _decide(fenced_session, open_record.approval_id)


# ── withdraw ──────────────────────────────────────────────────────────────────


def test_withdraw_writes_revoked_with_identity_provenance(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    seen: list[Any] = []
    open_record = _request(fenced_session)
    withdrawn = RepositoryApprovalAuthority.withdraw(
        fenced_session.connection_token,
        approval_id=open_record.approval_id,
        requested_by=fenced_session.identity_id,
        now=NOW + timedelta(minutes=2),
        record=seen.append,
    )
    assert withdrawn.decision == "revoked"
    assert withdrawn.decided_at == NOW + timedelta(minutes=2)
    assert withdrawn.revocation_actor_kind == "identity"
    assert withdrawn.revoked_by_identity_id == fenced_session.identity_id
    assert withdrawn.revocation_event_id is not None and len(withdrawn.revocation_event_id) == 36
    assert seen == [withdrawn]


def test_withdraw_requires_the_requester(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    open_record = _request(fenced_session)
    with pytest.raises(ApprovalWithdrawRequiresRequester, match="approver"):
        RepositoryApprovalAuthority.withdraw(
            fenced_session.connection_token, approval_id=open_record.approval_id, requested_by="approver", now=NOW, record=_ignore
        )


def test_withdraw_after_decision_reports_the_current_state(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    open_record = _request(fenced_session)
    _decide(fenced_session, open_record.approval_id)
    with pytest.raises(ApprovalAlreadyDecided, match="approved"):
        RepositoryApprovalAuthority.withdraw(
            fenced_session.connection_token, approval_id=open_record.approval_id, requested_by=fenced_session.identity_id, now=NOW, record=_ignore
        )


# ── supersede and approved_binding ────────────────────────────────────────────


def test_supersede_open_marks_only_this_sessions_open_requests(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    open_record = _request(fenced_session)
    decided = _request(fenced_session, state_id="state-2")
    _decide(fenced_session, decided.approval_id)
    superseded = RepositoryApprovalAuthority.supersede_open(fenced_session.connection_token, session_id=fenced_session.session_id, now=NOW)
    assert superseded == (open_record.approval_id,)
    rows = {r.approval_id: (r.decision, r.decided_at) for r in conn.execute(select(approvals_table)).all()}
    assert rows[open_record.approval_id] == ("superseded", NOW)
    assert rows[decided.approval_id][0] == "approved"
    assert RepositoryApprovalAuthority.supersede_open(fenced_session.connection_token, session_id=fenced_session.session_id, now=NOW) == ()


def test_approved_binding_returns_only_an_approved_row_for_the_exact_state(fenced_session: Any) -> None:
    _seed_approver(fenced_session)
    token = fenced_session.connection_token
    assert RepositoryApprovalAuthority.approved_binding(token, session_id=fenced_session.session_id, state_id="state-1") is None
    open_record = _request(fenced_session)
    assert RepositoryApprovalAuthority.approved_binding(token, session_id=fenced_session.session_id, state_id="state-1") is None
    _decide(fenced_session, open_record.approval_id)
    assert RepositoryApprovalAuthority.approved_binding(token, session_id=fenced_session.session_id, state_id="state-1") == _binding()
    assert RepositoryApprovalAuthority.approved_binding(token, session_id=fenced_session.session_id, state_id="state-2") is None
    RepositoryApprovalAuthority.supersede_open(token, session_id=fenced_session.session_id, now=NOW)
    assert RepositoryApprovalAuthority.approved_binding(token, session_id=fenced_session.session_id, state_id="state-1") == _binding(), (
        "supersede touches OPEN rows only; an approved row survives until a NEW state is written and a new request is made"
    )
```

- [ ] **Step 9: Run the authority tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_approval_authority.py -n 0 > /tmp/i3-lane-authority-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (collection error) with `ModuleNotFoundError: No module named 'elspeth.web.coordination.approval_authority'`.

- [ ] **Step 10: Write the authority module and the population-lock helper.**

First, in `src/elspeth/web/coordination/identity_authority.py`, directly after `_ADMIN_HOLDER_ROWS_FOR_UPDATE: Final = _ADMIN_HOLDER_ROWS.with_for_update()` (:797):

```python


def lock_admin_population(conn: Connection) -> None:
    """Take R5's population lock on ``conn`` (a SELECT ... FOR UPDATE, no write).

    Exposed for the approval authority: a mutation that can lock an
    administrator's ``identities`` row must take this FIRST, in the order the
    constant above documents, or it can deadlock a concurrent disable. Reads
    only; the manifest's write scan does not see it.
    """
    conn.execute(_ADMIN_HOLDER_ROWS_FOR_UPDATE).all()
```

Then create `src/elspeth/web/coordination/approval_authority.py`:

```python
"""Sole writer of ``approvals`` and ``approval_decisions`` (sso-design.md:1416-1417).

THE SEAM.  Sessions-side authorities never open the Landscape.  ``request``,
``decide`` and ``withdraw`` take a required ``record`` callback and invoke it
after the rows are written and BEFORE the caller's transaction commits (the
``identity_authority.py`` rule), so a failed audit write rolls the row back
(R4).  ``supersede_open`` takes no callback: its callers are the
composition-state head writers below the audit seam, exactly where
``RepositoryApprovalLifecycleAuthority.apply`` already writes ``revoked`` with
no Landscape row of its own; the transition is recorded on the sessions row.

LOCK ORDER (``approval_lifecycle_authority.py:3-8``).  The caller holds the
session lock (``ApprovalTransactionAuthority.run`` or the operation fence).
Inside it: if the REQUESTER holds deployment-wide ``admin`` (R8 bars an admin
approver), take identity authority's admin-population lock first; then both
participating ``identities`` rows ``FOR UPDATE`` in stable identity-id order;
then the approval rows.  Non-admin participants use stable id order alone.

CONDITIONAL WRITES.  ``decide`` and ``withdraw`` update
``WHERE decision IS NULL`` — never ``decided_at IS NULL`` — because
``superseded`` and ``revoked`` set ``decision`` without a stated timestamp
(spec :1416); ``rowcount == 0`` re-reads the row and raises
:class:`ApprovalAlreadyDecided` carrying the current state, one type for
every arm.

Every timestamp written here is the sessions database's clock, handed in as
``now`` by the caller that read it (``database_now``), never ``datetime.now``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, astuple, dataclass, fields
from datetime import datetime
from typing import Any, Final, Literal, final

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.engine import Connection, Engine, Row
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import lock_admin_population
from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import approval_decisions_table, approvals_table, identities_table, identity_roles_table

ApprovalDecision = Literal["approved", "rejected", "revoked", "superseded"]
IdentityDecision = Literal["approved", "rejected"]

MAX_APPROVAL_NOTE_BYTES: Final = 4096
"""Spec :1416: notes are bounded plain text, 4 KiB, enforced at the write boundary (models.py:3575-3578)."""


# ---------------------------------------------------------------------------
# The binding: a closed decision over WebPluginPolicyEvidence.
# ---------------------------------------------------------------------------

BOUND_EVIDENCE_FIELDS: Final[tuple[str, ...]] = ("binding_generation_fingerprint", "policy_hash")
"""Evidence fields copied into the binding. ``binding_generation_fingerprint`` is
in because ``config_hash`` records profile ALIASES, not the buckets or
credentials they resolve to (models.py:3596-3600)."""

EXCLUDED_EVIDENCE_FIELDS: Final[Mapping[str, str]] = {
    "schema_version": "a version of the evidence DTO, not of the pipeline",
    "snapshot_hash": "embeds the principal scope and would never match across approver and author (spec :1416)",
    "authorized_plugin_ids": "covered by policy_hash",
    "available_plugin_ids": "covered by policy_hash",
    "control_modes": "covered by policy_hash",
    "selected_implementations": "covered by binding_generation_fingerprint",
    "selected_profile_aliases": "covered by config_hash (aliases) and binding_generation_fingerprint (resolution)",
    "plugin_code_identities": "covered by binding_generation_fingerprint",
    "decision_codes": "an outcome of the policy walk, not an input to the run",
    "admission_decision": "the quota verdict of one run, never part of what an approver approves",
}
"""Every evidence field NOT in the binding, with the reason. The test pins
``set(BOUND) | set(EXCLUDED) == every field`` so a new field cannot enter the
DTO without a recorded decision."""


@final
@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    """The six keys of ``approvals.binding_json`` (models.py:3596-3603)."""

    config_hash: str
    canonical_version: str
    runtime_val_manifest_sha256: str
    openrouter_catalog_sha256: str
    binding_generation_fingerprint: str
    policy_hash: str

    def __post_init__(self) -> None:
        # astuple/fields, not getattr: the masquerade gate (CONTRIBUTING.md
        # "Gate: masquerade sites") inventories every getattr in the tree.
        for field, value in zip(fields(self), astuple(self), strict=True):
            if type(value) is not str or not value.strip():
                raise ValueError(f"ApprovalBinding.{field.name} must be a nonblank exact string")

    def as_json(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ApprovalBinding:
        if not isinstance(value, Mapping):
            raise ValueError("binding_json must be a mapping")
        values: dict[str, str] = {}
        for field in fields(cls):
            if field.name not in value:
                raise ValueError(f"binding_json is missing {field.name}")
            item = value[field.name]
            if type(item) is not str:
                raise ValueError(f"binding_json.{field.name} must be a string")
            values[field.name] = item
        if set(value) - set(values):
            raise ValueError(f"binding_json carries unknown keys {sorted(set(value) - set(values))}")
        return cls(**values)


def build_approval_binding(
    *,
    evidence: WebPluginPolicyEvidence,
    config_hash: str,
    canonical_version: str,
    openrouter_catalog_sha256: str,
    runtime_val_manifest_sha256: str,
) -> ApprovalBinding:
    """Compile the binding tuple.

    The two evidence fields are read by direct attribute access; the closed
    decision over every other evidence field is BOUND_EVIDENCE_FIELDS /
    EXCLUDED_EVIDENCE_FIELDS, pinned by the field-gate test, so adding a bound
    field means editing this body AND that tuple.
    """
    if not isinstance(evidence, WebPluginPolicyEvidence):
        raise TypeError("evidence must be an owned WebPluginPolicyEvidence")
    return ApprovalBinding(
        config_hash=config_hash,
        canonical_version=canonical_version,
        runtime_val_manifest_sha256=runtime_val_manifest_sha256,
        openrouter_catalog_sha256=openrouter_catalog_sha256,
        binding_generation_fingerprint=evidence.binding_generation_fingerprint,
        policy_hash=evidence.policy_hash,
    )


def runtime_val_manifest_sha256() -> str:
    """The manifest hash the run header will carry, computed in the web process.

    ``build_runtime_val_manifest`` refuses unfrozen registries
    (runtime_val_manifest.py:1715-1725); ``prepare_for_run`` freezes them once
    and is idempotent (bootstrap.py:30), and it is what the first run in this
    process would call anyway.
    """
    from elspeth.contracts.runtime_val_manifest import build_runtime_val_manifest
    from elspeth.engine.orchestrator.bootstrap import prepare_for_run

    prepare_for_run()
    return hashlib.sha256(canonical_json(build_runtime_val_manifest()).encode("utf-8")).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class ApprovalGateInputs:
    """What the permit path needs to compile the binding; ``None`` at the call site means governance is off."""

    evidence: WebPluginPolicyEvidence
    config_hash: str
    canonical_version: str
    openrouter_catalog_sha256: str
    runtime_val_manifest_sha256: str

    @property
    def binding(self) -> ApprovalBinding:
        return build_approval_binding(
            evidence=self.evidence,
            config_hash=self.config_hash,
            canonical_version=self.canonical_version,
            openrouter_catalog_sha256=self.openrouter_catalog_sha256,
            runtime_val_manifest_sha256=self.runtime_val_manifest_sha256,
        )


def evaluate_approval_gate(*, approved: ApprovalBinding | None, compiled: ApprovalBinding) -> AdmissionRefusalReason | None:
    """R2 (spec :1060): no approved row → APPROVAL_REQUIRED; a differing row → APPROVAL_BINDING_MISMATCH."""
    if approved is None:
        return AdmissionRefusalReason.APPROVAL_REQUIRED
    if approved != compiled:
        return AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH
    return None


# ---------------------------------------------------------------------------
# Records and refusals.
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    state_id: str
    binding: ApprovalBinding
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: ApprovalDecision | None
    request_note: str | None
    decision_seen_at: datetime | None
    decided_by_identity_id: str | None
    decision_note: str | None
    revoked_by_identity_id: str | None
    revocation_actor_kind: str | None
    revocation_event_id: str | None


class ApprovalRefusal(RuntimeError):
    """Base of the closed refusal set the routes translate."""


class ApprovalNotFound(ApprovalRefusal):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval {approval_id} not found")


class ApprovalAlreadyDecided(ApprovalRefusal):
    def __init__(self, current_state: str) -> None:
        self.current_state = current_state
        super().__init__(f"approval is already {current_state}")


class ApprovalAuthorIsApprover(ApprovalRefusal):
    def __init__(self, identity_id: str) -> None:
        super().__init__(f"{identity_id} is the author of this state and cannot be its approver")


class ApproverRoleRequired(ApprovalRefusal):
    def __init__(self, identity_id: str) -> None:
        super().__init__(f"{identity_id} does not hold an active deployment-wide approver role")


class ApprovalParticipantNotActive(ApprovalRefusal):
    def __init__(self, identity_id: str) -> None:
        super().__init__(f"{identity_id} is not an active identity")


class ApprovalOpenRequestExists(ApprovalRefusal):
    def __init__(self, session_id: str, state_id: str) -> None:
        super().__init__(f"an open approval request already exists for state {state_id} of session {session_id}")


class ApprovalNoteRequired(ApprovalRefusal):
    def __init__(self, decision: str) -> None:
        super().__init__(f"a {decision} decision must carry a non-blank note")


class ApprovalNoteTooLong(ApprovalRefusal):
    def __init__(self, size: int) -> None:
        super().__init__(f"note is {size} bytes; the bound is {MAX_APPROVAL_NOTE_BYTES}")


class ApprovalWithdrawRequiresRequester(ApprovalRefusal):
    def __init__(self, identity_id: str) -> None:
        super().__init__(f"{identity_id} did not request this approval and cannot withdraw it")


# ---------------------------------------------------------------------------
# Statements. Module-level constants with bound parameters.
# ---------------------------------------------------------------------------

_IDENTITY_FOR_UPDATE: Final = select(identities_table).where(identities_table.c.identity_id == bindparam("identity_id")).with_for_update()
_ROLES_OF_IDENTITY: Final = select(identity_roles_table).where(identity_roles_table.c.identity_id == bindparam("identity_id"))
_APPROVAL_BY_ID: Final = select(approvals_table).where(approvals_table.c.approval_id == bindparam("approval_id"))
_APPROVAL_BY_ID_FOR_UPDATE: Final = _APPROVAL_BY_ID.with_for_update()
_APPROVED_FOR_STATE_FOR_UPDATE: Final = (
    select(approvals_table)
    .where(
        approvals_table.c.session_id == bindparam("session_id"),
        approvals_table.c.state_id == bindparam("state_id"),
        approvals_table.c.decision == "approved",
    )
    .with_for_update()
)
_APPROVED_FOR_STATE: Final = select(approvals_table).where(
    approvals_table.c.session_id == bindparam("session_id"),
    approvals_table.c.state_id == bindparam("state_id"),
    approvals_table.c.decision == "approved",
)
_OPEN_FOR_SESSION: Final = select(approvals_table.c.approval_id).where(
    approvals_table.c.session_id == bindparam("session_id"),
    approvals_table.c.decision.is_(None),
)
_DECISION_ROW: Final = (
    select(approval_decisions_table)
    .where(approval_decisions_table.c.approval_id == bindparam("approval_id"))
    .order_by(approval_decisions_table.c.decided_at)
)
_INBOX: Final = (
    select(approvals_table)
    .where(approvals_table.c.approver_identity_id == bindparam("identity_id"), approvals_table.c.decision.is_(None))
    .order_by(approvals_table.c.requested_at.desc())
)
_SENT: Final = (
    select(approvals_table)
    .where(approvals_table.c.requested_by_identity_id == bindparam("identity_id"))
    .order_by(approvals_table.c.requested_at.desc())
)


def _lock_identity_row(conn: Connection, identity_id: str) -> Row[Any]:
    row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
    if row is None or row.access_state != "active":
        raise ApprovalParticipantNotActive(identity_id)
    return row


def _active_grants(conn: Connection, identity_id: str, now: datetime) -> list[Row[Any]]:
    rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
    return [
        row
        for row in rows
        if row.revoked_at is None and (row.expires_at is None or _aware(row.expires_at) > now)
    ]


def _aware(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _holds(grants: list[Row[Any]], role: str) -> bool:
    return any(grant.role == role and grant.scope is None for grant in grants)


def _bounded_note(note: str | None) -> str | None:
    if note is None:
        return None
    size = len(note.encode("utf-8"))
    if size > MAX_APPROVAL_NOTE_BYTES:
        raise ApprovalNoteTooLong(size)
    return note


def _record(conn: Connection, row: Row[Any]) -> ApprovalRecord:
    decisions = conn.execute(_DECISION_ROW, {"approval_id": row.approval_id}).all()
    last = decisions[-1] if decisions else None
    return ApprovalRecord(
        approval_id=row.approval_id,
        session_id=row.session_id,
        state_id=row.state_id,
        binding=ApprovalBinding.from_json(row.binding_json),
        requested_by_identity_id=row.requested_by_identity_id,
        approver_identity_id=row.approver_identity_id,
        requested_at=_aware(row.requested_at),
        decided_at=None if row.decided_at is None else _aware(row.decided_at),
        decision=row.decision,
        request_note=row.request_note,
        decision_seen_at=None if row.decision_seen_at is None else _aware(row.decision_seen_at),
        decided_by_identity_id=None if last is None else last.decided_by_identity_id,
        decision_note=None if last is None else last.note,
        revoked_by_identity_id=row.revoked_by_identity_id,
        revocation_actor_kind=row.revocation_actor_kind,
        revocation_event_id=row.revocation_event_id,
    )


def _lock_participants(conn: Connection, *, requester: str, approver: str, now: datetime) -> None:
    """Population lock first when the requester holds admin; then the two rows in stable id order."""
    if requester == approver:
        raise ApprovalAuthorIsApprover(requester)
    if _holds(_active_grants(conn, requester, now), "admin"):
        lock_admin_population(conn)
    for identity_id in sorted((requester, approver)):
        _lock_identity_row(conn, identity_id)
    if not _holds(_active_grants(conn, approver, now), "approver"):
        raise ApproverRoleRequired(approver)


def supersede_open_approvals(connection: Connection, *, session_id: str, now: datetime) -> tuple[str, ...]:
    """Spec :1416: any new ``state_id`` marks the open request superseded. The ONLY writer of that value.

    Called by every composition-state head writer on the connection that
    inserted the head, so the advance and the retirement commit together —
    the same shape as ``supersede_dead_site_pending_interpretation_events``.
    """
    open_ids = tuple(row.approval_id for row in connection.execute(_OPEN_FOR_SESSION, {"session_id": session_id}).all())
    if not open_ids:
        return ()
    connection.execute(
        update(approvals_table)
        .where(approvals_table.c.session_id == session_id, approvals_table.c.decision.is_(None))
        .values(decision="superseded", decided_at=now)
    )
    return open_ids


@final
class RepositoryApprovalAuthority:
    """Token-based: every method runs inside the caller's already-locked transaction. No connection escapes."""

    @staticmethod
    def request(
        connection_token: str,
        *,
        session_id: str,
        state_id: str,
        binding: ApprovalBinding,
        requested_by: str,
        approver: str,
        note: str | None,
        now: datetime,
        record: Callable[[ApprovalRecord], None],
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        if type(binding) is not ApprovalBinding:
            raise TypeError("binding must be an exact ApprovalBinding")
        bounded_note = _bounded_note(note)
        _lock_participants(conn, requester=requested_by, approver=approver, now=now)
        approval_id = str(uuid.uuid4())
        try:
            with conn.begin_nested():
                conn.execute(
                    insert(approvals_table).values(
                        approval_id=approval_id,
                        session_id=session_id,
                        state_id=state_id,
                        binding_json=binding.as_json(),
                        requested_by_identity_id=requested_by,
                        approver_identity_id=approver,
                        requested_at=now,
                        request_note=bounded_note,
                    )
                )
        except IntegrityError:
            # uq_approvals_open_per_state (models.py:3660-3666): one OPEN request per (session, state).
            raise ApprovalOpenRequestExists(session_id, state_id) from None
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        record(result)
        return result

    @staticmethod
    def decide(
        connection_token: str,
        *,
        approval_id: str,
        decided_by: str,
        decision: IdentityDecision,
        note: str | None,
        now: datetime,
        record: Callable[[ApprovalRecord], None],
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        if decision not in ("approved", "rejected"):
            raise ValueError(f"decision must be approved or rejected, not {decision!r}")
        bounded_note = _bounded_note(note)
        if decision == "rejected" and (bounded_note is None or not bounded_note.strip()):
            raise ApprovalNoteRequired(decision)
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound(approval_id)
        _lock_participants(conn, requester=row.requested_by_identity_id, approver=decided_by, now=now)
        locked = conn.execute(_APPROVAL_BY_ID_FOR_UPDATE, {"approval_id": approval_id}).one()
        if locked.decision is not None:
            raise ApprovalAlreadyDecided(locked.decision)
        conn.execute(
            insert(approval_decisions_table).values(
                decision_id=str(uuid.uuid4()),
                approval_id=approval_id,
                decided_by_identity_id=decided_by,
                decided_at=now,
                decision=decision,
                note=bounded_note,
            )
        )
        outcome = conn.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == approval_id, approvals_table.c.decision.is_(None))
            .values(decision=decision, decided_at=now)
        )
        if outcome.rowcount != 1:
            current = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one()
            raise ApprovalAlreadyDecided(current.decision or "open")
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        record(result)
        return result

    @staticmethod
    def withdraw(
        connection_token: str,
        *,
        approval_id: str,
        requested_by: str,
        now: datetime,
        record: Callable[[ApprovalRecord], None],
    ) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVAL_BY_ID_FOR_UPDATE, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row.requested_by_identity_id != requested_by:
            raise ApprovalWithdrawRequiresRequester(requested_by)
        if row.decision is not None:
            raise ApprovalAlreadyDecided(row.decision)
        # ck_approvals_revocation_provenance (models.py:3639-3645): a revoked row by an
        # identity carries decided_at, a fresh event id, actor_kind and the identity.
        outcome = conn.execute(
            update(approvals_table)
            .where(
                approvals_table.c.approval_id == approval_id,
                approvals_table.c.decision.is_(None),
                approvals_table.c.requested_by_identity_id == requested_by,
            )
            .values(
                decision="revoked",
                decided_at=now,
                revocation_actor_kind="identity",
                revoked_by_identity_id=requested_by,
                revocation_event_id=str(uuid.uuid4()),
            )
        )
        if outcome.rowcount != 1:
            current = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one()
            raise ApprovalAlreadyDecided(current.decision or "open")
        result = _record(conn, conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one())
        record(result)
        return result

    @staticmethod
    def supersede_open(connection_token: str, *, session_id: str, now: datetime) -> tuple[str, ...]:
        return supersede_open_approvals(_resolve_mutation_connection(connection_token), session_id=session_id, now=now)

    @staticmethod
    def approved_binding(connection_token: str, *, session_id: str, state_id: str) -> ApprovalBinding | None:
        """The approved row for this exact state, locked so a concurrent withdrawal waits for the permit decision."""
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVED_FOR_STATE_FOR_UPDATE, {"session_id": session_id, "state_id": state_id}).one_or_none()
        return None if row is None else ApprovalBinding.from_json(row.binding_json)

    @staticmethod
    def read(connection_token: str, *, approval_id: str) -> ApprovalRecord:
        conn = _resolve_mutation_connection(connection_token)
        row = conn.execute(_APPROVAL_BY_ID, {"approval_id": approval_id}).one_or_none()
        if row is None:
            raise ApprovalNotFound(approval_id)
        return _record(conn, row)


@final
class ApprovalTransactionAuthority:
    """The one place a workflow route opens a sessions transaction for approvals.

    ``run`` hands the mutation a TOKEN and the database clock, never the
    connection (the ``_emit_authority_revoked`` shape at
    identity_authority.py:1233-1238). Reads use a plain connection.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def run[T](self, session_id: str, mutation: Callable[[str, datetime], T]) -> T:
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        with locked_session_transaction(self._engine, session_id) as conn:
            now = database_now(conn)
            token = _register_mutation_connection(conn)
            try:
                return mutation(token, now)
            finally:
                _unregister_mutation_connection(token)

    def session_id_of(self, approval_id: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(approvals_table.c.session_id).where(approvals_table.c.approval_id == approval_id)).one_or_none()
        return None if row is None else row.session_id

    def approved_binding(self, *, session_id: str, state_id: str) -> ApprovalBinding | None:
        with self._engine.connect() as conn:
            row = conn.execute(_APPROVED_FOR_STATE, {"session_id": session_id, "state_id": state_id}).one_or_none()
        return None if row is None else ApprovalBinding.from_json(row.binding_json)

    def inbox(self, *, approver_identity_id: str) -> tuple[ApprovalRecord, ...]:
        with self._engine.connect() as conn:
            return tuple(_record(conn, row) for row in conn.execute(_INBOX, {"identity_id": approver_identity_id}).all())

    def sent(self, *, requested_by_identity_id: str) -> tuple[ApprovalRecord, ...]:
        with self._engine.connect() as conn:
            return tuple(_record(conn, row) for row in conn.execute(_SENT, {"identity_id": requested_by_identity_id}).all())
```

- [ ] **Step 11: Run the authority and audit tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_approval_authority.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i3-lane-authority-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. If `test_request_bounds_the_note_at_4096_bytes` fails on the SAVEPOINT (`begin_nested`) under the I1 fixture's connection, the fixture opened the transaction with `engine.begin()` and SQLite savepoints are fine; if it opened `engine.connect()` without a transaction, replace `with conn.begin_nested():` by a pre-check `SELECT 1 FROM approvals WHERE session_id=:s AND state_id=:t AND decision IS NULL` raising `ApprovalOpenRequestExists` before the INSERT and keep the `except IntegrityError` arm for the PostgreSQL race.

- [ ] **Step 12: Bind the new writers in the mutation-authority manifest and prove the gate is clean.**

In `tests/unit/architecture/test_session_db_mutation_authority.py`, `_NAMED_AUTHORITY_SYMBOLS` (:262), directly after the `RepositoryApprovalLifecycleAuthority.apply` entry (:268-272):

```python
    # Task I3: the approval product. Class-prefix binding covers request /
    # decide / withdraw / supersede_open / approved_binding / read; the
    # module-level supersede writer is the shared retirement every
    # composition-state head writer calls (dead_site_supersession shape).
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "supersede_open_approvals",
        "ApprovalAuthority",
    ),
    AuthoritySymbol(
        "src/elspeth/web/coordination/approval_authority.py",
        "ApprovalTransactionAuthority.run",
        "ApprovalAuthority",
    ),
```

Then run the gate and read its XFAIL text:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -rx > /tmp/i3-lane-manifest-1.log 2>&1; echo exit=$?`
Expected: `exit=0` but the log shows `XFAIL` with `Unexpected/unreviewed (6):` listing, each with its `fp=<16 hex>#<ordinal>` and `line=`:
`approval_authority.py:<line> supersede_open_approvals update approvals`, `RepositoryApprovalAuthority.request insert approvals`, `RepositoryApprovalAuthority.decide insert approval_decisions`, `RepositoryApprovalAuthority.decide update approvals`, `RepositoryApprovalAuthority.withdraw update approvals`, `ApprovalTransactionAuthority.run write_connection <sessions-write-connection>`; and `Stale reviewed (0)`.

Add one `WriterIdentity(...)` per listed site to `_REVIEWED_WRITERS` (:1198), directly after the `RepositoryApprovalLifecycleAuthority.apply` row (:1245-1254), copying `path`, `symbol`, `table`, `operation`, the printed fingerprint, ordinal and line, with authority `"ApprovalAuthority"`:

```python
    # Task I3: the approval product (sso-design.md:1416-1417). request INSERTs
    # under both identity locks; decide INSERTs the decision row and UPDATEs
    # WHERE decision IS NULL; withdraw UPDATEs to revoked with identity
    # provenance; supersede_open_approvals is the only writer of 'superseded'.
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "supersede_open_approvals",
        "approvals",
        "update",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.request",
        "approvals",
        "insert",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.decide",
        "approval_decisions",
        "insert",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.decide",
        "approvals",
        "update",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "RepositoryApprovalAuthority.withdraw",
        "approvals",
        "update",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/approval_authority.py",
        "ApprovalTransactionAuthority.run",
        "<sessions-write-connection>",
        "write_connection",
        "<fp from the log>",
        1,
        "ApprovalAuthority",
        line=<line from the log>,
    ),
```

The `<fp from the log>` and `<line from the log>` placeholders are filled from the XFAIL text, never typed from memory: the fingerprint is the AST of the enclosing function and any later edit to that function (Steps 15 and 19 do not touch this file) would print a new one. Re-run the same command with its log path changed to `/tmp/i3-lane-manifest-2.log`; expected: `exit=0` and `1 xfailed`: the gate already XFAILs on a clean HEAD (measured 2026-09-14 on a `git archive` export of 818d04577, `1 xfailed in 115.45s`), so this task cannot bring it to a pass. Read the XFAIL text instead: its counts must equal I1 Step 15's recorded baseline, and no `Unexpected/unreviewed` or `Stale reviewed` row may name a site this task touches (`grep -c 'ApprovalAuthority' /tmp/i3-lane-manifest-2.log` prints `0`). A `Stale reviewed` line means a copied fingerprint or line is wrong.

- [ ] **Step 13: Write the failing R2 permit-path tests.**

Create `tests/unit/web/coordination/test_r2_execute_gate.py`. The `_admission`
helper is the one `test_durable_run_admission.py:85-130` uses, reproduced
here in full so this module drives the widened facet exactly the way the
execution service will:

```python
"""R2 (sso-design.md:1060) inside ``RepositoryRunStartPermitAuthority._assess``: fire and mutation-derivation."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, QuotaDisposition
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.approval_authority import ApprovalGateInputs, build_approval_binding
from elspeth.web.coordination.contracts import SessionOperationKind, StartPermitState
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.envelope import RunExecutionInput
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import approvals_table, composition_states_table, identity_roles_table, run_events_table, runs_table

NO_QUOTA_POLICY = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _evidence() -> WebPluginPolicyEvidence:
    return WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="a" * 64,
        snapshot_hash="b" * 64,
        authorized_plugin_ids=("sink:csv", "source:csv"),
        available_plugin_ids=("sink:csv", "source:csv"),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(),
        binding_generation_fingerprint="c" * 64,
        decision_codes=("policy_allowed",),
    )


def _gate() -> ApprovalGateInputs:
    return ApprovalGateInputs(
        evidence=_evidence(),
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )


def _admission(engine: Any) -> tuple[Any, Any, Any, Any]:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        ensure_test_identity(conn, identity_id="approver")
        # Idempotent: a test may admit twice on one engine.
        if conn.execute(select(identity_roles_table.c.role_id).where(identity_roles_table.c.role_id == "approver-role")).first() is None:
            conn.execute(
                insert(identity_roles_table).values(
                    role_id="approver-role", identity_id="approver", role="approver", granted_at=NOW, granted_by_identity_id="approver"
                )
            )
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="admission", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    fence = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="owner", lease_seconds=30
    )
    context = fence
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id), session_id=str(session.id), version=1, provenance="session_seed", created_at=datetime.now(UTC)
            )
        )
    envelope = RunExecutionInput(
        schema_version=1,
        envelope_json="{}",
        canonical_input_digest="a" * 64,
        topology_digest="b" * 64,
        source_manifest_digest="c" * 64,
        application_fingerprint="d" * 64,
        plugin_registry_fingerprint="e" * 64,
        configuration_fingerprint="f" * 64,
        graph_fingerprint="a" * 64,
        runtime_fingerprint="b" * 64,
        implementation_fingerprint="c" * 64,
        deployment_generation="test",
        session_epoch=1,
        landscape_epoch=1,
        coordination_protocol=1,
        automatic_recovery_eligible=True,
    )
    run_id = uuid4()

    def admit(tx: Any) -> Any:
        return tx.runs.create_pending_run(
            run_id=run_id, state_id=state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=envelope
        )

    run = authority.mutate(context, admit)
    return authority, context, run, state_id


def _approve(engine: Any, run: Any, state_id: Any, binding: Any) -> str:
    approval_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id=approval_id,
                session_id=str(run.session_id),
                state_id=str(state_id),
                binding_json=binding.as_json(),
                requested_by_identity_id="alice",
                approver_identity_id="approver",
                requested_at=NOW,
                decided_at=NOW,
                decision="approved",
            )
        )
    return approval_id


def test_execute_refuses_without_an_approved_matching_binding(engine: Any) -> None:
    authority, context, run, _state_id = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert permit.state is StartPermitState.REFUSED
    assert permit.admission_decision is not None
    assert permit.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED
    assert permit.admission_decision.evidence.quota_disposition is QuotaDisposition.NOT_ASSESSED
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.saga_state)).scalar_one() == "admission_refusal_pending"
        assert conn.execute(select(runs_table.c.error)).scalar_one() == "Run admission refused: approval_required"
        event = conn.execute(select(run_events_table)).one()
        assert event.event_type == "failed" and "approval_required" in event.data["detail"]


def test_execute_permits_with_an_approved_matching_binding(engine: Any) -> None:
    authority, context, run, state_id = _admission(engine)
    _approve(engine, run, state_id, _gate().binding)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert permit.state is StartPermitState.START_PERMITTED
    assert permit.admission_decision is not None and permit.admission_decision.allowed


def test_execute_refuses_when_the_approved_binding_differs_from_the_compiled_one(engine: Any) -> None:
    authority, context, run, state_id = _admission(engine)
    _approve(engine, run, state_id, dataclasses.replace(_gate().binding, openrouter_catalog_sha256="9" * 64))
    permit = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert permit.state is StartPermitState.REFUSED
    assert permit.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH


def test_r2_derives_from_the_binding_tuple(engine: Any) -> None:
    """Mutation-derivation: flip binding_generation_fingerprint on the APPROVED row only → refused; restore → permitted."""
    authority, context, run, state_id = _admission(engine)
    _approve(engine, run, state_id, dataclasses.replace(_gate().binding, binding_generation_fingerprint="9" * 64))
    refused = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert refused.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH
    # A refused permit is terminal for this run: the SAME compiled gate against a
    # second run whose approved row carries the restored fingerprint is permitted.
    authority2, context2, run2, state_id2 = _admission(engine)
    _approve(engine, run2, state_id2, _gate().binding)
    permitted = authority2.mutate(context2, lambda tx: tx.runs.issue_start_permit(run_id=run2.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert permitted.state is StartPermitState.START_PERMITTED


def test_r2_is_inert_when_the_caller_passes_no_gate(engine: Any) -> None:
    """``approval=None`` is the governance-off contract: the permit path never reads approvals."""
    authority, context, run, _state_id = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY, approval=None))
    assert permit.state is StartPermitState.START_PERMITTED


def test_r2_defaults_to_no_gate_so_existing_callers_are_unchanged(engine: Any) -> None:
    authority, context, run, _state_id = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert permit.state is StartPermitState.START_PERMITTED


def test_recovery_reassessment_refuses_a_permit_whose_approval_was_withdrawn(engine: Any) -> None:
    """R2's second clause (:1061): a matching row that is later revoked refuses the run on re-assessment."""
    from sqlalchemy import update

    authority, context, run, state_id = _admission(engine)
    approval_id = _approve(engine, run, state_id, _gate().binding)
    issued = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert issued.state is StartPermitState.START_PERMITTED
    with engine.begin() as conn:
        conn.execute(
            update(approvals_table)
            .where(approvals_table.c.approval_id == approval_id)
            .values(decision="revoked", revocation_actor_kind="identity", revoked_by_identity_id="alice", revocation_event_id="e-1")
        )
    reassessed = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert reassessed.state is StartPermitState.START_PERMITTED, "the historical allowance is preserved"
    assert reassessed.execution_refusal is not None
    assert reassessed.execution_refusal.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED


def test_quota_refusal_wins_over_the_approval_gate(engine: Any) -> None:
    """Order inside _assess: chargeable admission first (:41), R2 only on an allowed decision."""
    from sqlalchemy import update

    from elspeth.web.sessions.models import identities_table

    authority, context, run, _state_id = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    permit = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY, approval=_gate()))
    assert permit.admission_decision.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
```

Also update the facet-signature pin in `tests/unit/web/sessions/test_operation_fence_wiring.py:476-490`: both tuples gain a third entry
`("approval", inspect.Parameter.KEYWORD_ONLY, ApprovalGateInputs | None)` (import `ApprovalGateInputs` from `elspeth.web.coordination.approval_authority` at the top of that module; if `_required_parameter`'s comparison uses `is`, compare the annotation string `"ApprovalGateInputs | None"` the way the module already handles `"SessionOperationContext"` at :72).

- [ ] **Step 14: Run the R2 tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/sessions/test_operation_fence_wiring.py -n 0 > /tmp/i3-lane-r2-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; every `approval=` test fails with `TypeError: _RepositoryRunMutations.assess_start_admission() got an unexpected keyword argument 'approval'` (or `issue_start_permit`), `test_r2_defaults_to_no_gate_so_existing_callers_are_unchanged` passes, and the fence-wiring parametrised case fails on the missing third parameter.

- [ ] **Step 15: Widen the permit path.**

1. `src/elspeth/web/coordination/run_start_permit_authority.py`. Add the imports
   `from elspeth.web.coordination.approval_authority import ApprovalGateInputs, RepositoryApprovalAuthority, evaluate_approval_gate`.
   Replace `_assess` (:36-90) with:

```python
    @staticmethod
    def _assess(
        connection_token: str,
        *,
        run_id: str,
        context: SessionOperationContext,
        now: datetime,
        policy: ChargeableAdmissionPolicy,
        approval: ApprovalGateInputs | None,
    ) -> tuple[Row[Any], ChargeableAdmissionDecision]:
        conn = _resolve_mutation_connection(connection_token)
        decision = RepositoryChargeableAdmissionAuthority.assess(connection_token, session_id=context.fence.session_id, policy=policy)
        run = conn.execute(select(runs_table).where(runs_table.c.id == run_id).with_for_update()).one()
        if run.session_id != context.fence.session_id:
            raise AuditIntegrityError("Run permit session custody mismatch")
        if decision.allowed and approval is not None:
            # R2 (sso-design.md:1060): only an ``approved`` row for THIS state whose
            # binding equals the compiled one admits the run. ``None`` means the
            # deployment runs with workflow_governance="off" and the gate is
            # never consulted. Evaluated on every assessment, so a permit whose
            # approval was withdrawn afterwards refuses on recovery (:1061).
            approved = RepositoryApprovalAuthority.approved_binding(connection_token, session_id=run.session_id, state_id=run.state_id)
            refusal = evaluate_approval_gate(approved=approved, compiled=approval.binding)
            if refusal is not None:
                decision = ChargeableAdmissionDecision(
                    refusal_reason=refusal,
                    evidence=AdmissionPolicyEvidence(
                        quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                    ),
                )
        row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id).with_for_update()).one()
        if row.start_state in {"refused", "cancelled_before_permit"} or row.execution_refusal is not None:
            return row, decision
        if row.start_state == "start_permitted":
            previous = RepositoryRunStartPermitAuthority._record(row)
            assert previous.admission_decision is not None
            if decision.allowed and previous.admission_decision.evidence.secret_wiring_hash != policy.secret_wiring_hash:
                decision = ChargeableAdmissionDecision(
                    refusal_reason=AdmissionRefusalReason.POLICY_GENERATION_CHANGED,
                    evidence=AdmissionPolicyEvidence(
                        quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                    ),
                )
        if not decision.allowed:
            if row.start_state == "pending":
                conn.execute(
                    update(run_start_permits_table)
                    .where(run_start_permits_table.c.run_id == run_id)
                    .values(
                        start_state="refused",
                        admission_decision=decision.model_dump(mode="json"),
                        admission_decision_hash=decision.canonical_hash,
                        decided_at=now,
                    )
                )
            else:
                conn.execute(
                    update(run_start_permits_table)
                    .where(run_start_permits_table.c.run_id == run_id)
                    .values(
                        execution_refusal=decision.model_dump(mode="json"),
                    )
                )
            assert decision.refusal_reason is not None
            conn.execute(
                update(runs_table)
                .where(runs_table.c.id == run_id)
                .values(
                    status="failed",
                    saga_state="admission_refusal_pending",
                    finished_at=now,
                    error=f"Run admission refused: {decision.refusal_reason.value}",
                )
            )
            row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
        return row, decision
```

   Widen `assess` (:93) and `issue` (:101): each signature gains
   `approval: ApprovalGateInputs | None` after `policy`, and each `_assess(...)`
   call passes `approval=approval`. The body of `issue` below its `_assess` call
   is unchanged. (The `start_permitted` re-assessment keeps the historical
   allowance and records the R2 refusal as `execution_refusal`, which is the
   arm `test_recovery_reassessment_refuses_a_permit_whose_approval_was_withdrawn` pins.)

2. `src/elspeth/web/sessions/protocol.py:3392-3394`:

```python
    def issue_start_permit(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    def assess_start_admission(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...
```

   and `:4678-4682`:

```python
    async def issue_run_start_permit(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...

    async def assess_run_start_admission(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord: ...
```

   with `from elspeth.web.coordination.approval_authority import ApprovalGateInputs` under the module's `TYPE_CHECKING` imports (protocol.py must not import coordination at runtime; check the existing `if TYPE_CHECKING:` block and add it there).

3. `src/elspeth/web/coordination/repository.py:1552-1571`:

```python
    def assess_start_admission(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        state = self.__state
        state._require_active()
        context = self._require_execute()
        state._validate_uuid(run_id, field_name="run_id")
        if approval is not None and type(approval) is not ApprovalGateInputs:
            raise TypeError("approval must be an exact ApprovalGateInputs or None")
        permit = RepositoryRunStartPermitAuthority.assess(
            state._connection_token, run_id=str(run_id), context=context, now=state._database_now, policy=policy, approval=approval
        )
        self._record_admission_refusal(run_id, permit)
        return permit

    def issue_start_permit(
        self, *, run_id: UUID, policy: ChargeableAdmissionPolicy, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        state = self.__state
        state._require_active()
        context = self._require_execute()
        state._validate_uuid(run_id, field_name="run_id")
        if approval is not None and type(approval) is not ApprovalGateInputs:
            raise TypeError("approval must be an exact ApprovalGateInputs or None")
        permit = RepositoryRunStartPermitAuthority.issue(
            state._connection_token, run_id=str(run_id), context=context, now=state._database_now, policy=policy, approval=approval
        )
        self._record_admission_refusal(run_id, permit)
        return permit
```

   with `from elspeth.web.coordination.approval_authority import ApprovalGateInputs` added to the module imports.

4. `src/elspeth/web/sessions/service.py:10055-10073`:

```python
    async def assess_run_start_admission(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return cast(
            "RunStartPermitRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.assess_start_admission(
                    run_id=run_id, policy=self._chargeable_admission_policy, approval=approval
                ),
            ),
        )

    async def issue_run_start_permit(
        self, run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return cast(
            "RunStartPermitRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.issue_start_permit(
                    run_id=run_id, policy=self._chargeable_admission_policy, approval=approval
                ),
            ),
        )
```

5. `tests/unit/web/execution/test_service.py:751` and `:765`: both fakes gain
   `approval: Any = None` as a trailing keyword parameter:
   `async def issue_run_start_permit(run_id: UUID, *, session_operation_context: SessionOperationContext, approval: Any = None) -> RunStartPermitRecord:`
   and the same for `assess_run_start_admission`.

- [ ] **Step 16: Run the R2 suites and re-fingerprint `_assess` in the manifest.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/sessions/test_operation_fence_wiring.py tests/unit/web/execution/test_service.py -n 0 > /tmp/i3-lane-r2-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Then: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -rx > /tmp/i3-lane-manifest-3.log 2>&1; echo exit=$?`
Expected: XFAIL text with `Unexpected/unreviewed (3)` naming the three `RepositoryRunStartPermitAuthority._assess` sites (`run_start_permits update` ×2, `runs update` ×1) with a NEW fingerprint and new lines, and `Stale reviewed (3)` naming the old rows (`fp=2f5970706a950c82`, lines 61/72/80). Edit the three rows at `_REVIEWED_WRITERS` :1213-1246: replace the fingerprint and each `line=` with the printed values (the ordinals 1, 2, 1 and the comment above them stay). Re-run it with its log path changed to `/tmp/i3-lane-manifest-4.log`; expected `exit=0` and `1 xfailed`: the gate already XFAILs on a clean HEAD (measured 2026-09-14 on a `git archive` export of 818d04577, `1 xfailed in 115.45s`), so this task cannot bring it to a pass. Read the XFAIL text instead: its counts must equal I1 Step 15's recorded baseline, and no `Unexpected/unreviewed` or `Stale reviewed` row may name a site this task touches (`grep -c '_assess' /tmp/i3-lane-manifest-4.log` prints `0`).

- [ ] **Step 17: Write the failing supersede-on-new-state tests.**

Create `tests/unit/web/coordination/test_state_writers_supersede_approvals.py`:

```python
"""Spec :1416: any new ``state_id`` marks the open request superseded — at EVERY head writer.

Two instruments, covering the five composition-state head writers. The
behavioural half drives the four public writers and reads the ``approvals``
row back: ``save_composition_state`` → ``append_state``;
``save_composition_state_with_interpretations`` (empty cohort),
``commit_composition_response`` and ``save_state_for_guided_operation`` →
``_insert_composition_state`` (sessions/service.py:9803, :9910, :10959 on
072141b75). The fifth name, ``_insert_composition_state``, is the private
shared INSERT those three reach, so it is exercised by three behavioural
parameters and pinned directly by the structural half, which reads the source
of every ``insert(composition_states_table)`` site and pins that the
retirement call sits in the same function, after the INSERT
(``create_or_reconcile_pending`` needs an interpretation cohort no unit test
here can cheaply build).
"""

from __future__ import annotations

import inspect
import textwrap
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert, select
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination import repository as repository_module
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.models import approvals_table
from elspeth.web.sessions.protocol import CompositionStateData, GuidedOperationClaimed
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def service(engine: Any, tmp_path: Any) -> SessionServiceImpl:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        ensure_test_identity(conn, identity_id="approver")
    return SessionServiceImpl(engine, data_dir=tmp_path, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.i3"))


def _open_request(engine: Any, session_id: UUID, state_id: UUID) -> str:
    approval_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id=approval_id,
                session_id=str(session_id),
                state_id=str(state_id),
                binding_json={"config_hash": "1" * 64, "canonical_version": "sha256-rfc8785-v1", "runtime_val_manifest_sha256": "3" * 64,
                              "openrouter_catalog_sha256": "2" * 64, "binding_generation_fingerprint": "c" * 64, "policy_hash": "a" * 64},
                requested_by_identity_id="alice",
                approver_identity_id="approver",
                requested_at=NOW,
            )
        )
    return approval_id


def _decision(engine: Any, approval_id: str) -> tuple[str | None, datetime | None]:
    with engine.connect() as conn:
        row = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == approval_id)).one()
    return row.decision, row.decided_at


def _state() -> CompositionStateData:
    return CompositionStateData(sources={}, nodes=[], edges=[], outputs=[], metadata_={}, is_valid=False, validation_errors=None)


async def _compose_context(service: SessionServiceImpl, session_id: UUID) -> Any:
    from elspeth.web.coordination.lifecycle import SessionOperationLease

    return await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "writer",
    [
        "save_composition_state",
        "save_composition_state_with_interpretations",
        "commit_composition_response",
        "save_state_for_guided_operation",
    ],
)
async def test_every_state_writer_supersedes_the_open_request(service: SessionServiceImpl, engine: Any, writer: str) -> None:
    session = await service.create_session("alice", "supersede", "local")
    lease = await _compose_context(service, session.id)
    async with lease:
        first = await service.save_composition_state(session.id, _state(), provenance="session_seed", session_operation_context=lease.context)
        open_id = _open_request(engine, session.id, first.id)
        other_session = await service.create_session("alice", "other", "local")
        other_lease = await _compose_context(service, other_session.id)
        async with other_lease:
            other_state = await service.save_composition_state(
                other_session.id, _state(), provenance="session_seed", session_operation_context=other_lease.context
            )
        other_open = _open_request(engine, other_session.id, other_state.id)
        if writer == "save_composition_state":
            await service.save_composition_state(session.id, _state(), provenance="post_compose", session_operation_context=lease.context)
        elif writer == "save_composition_state_with_interpretations":
            # An empty cohort passes the exact-tuple check (service.py:9756-9757) and
            # still commits the state through _insert_composition_state (:9803).
            await service.save_composition_state_with_interpretations(
                session.id, _state(), provenance="post_compose", interpretations=(), session_operation_context=lease.context
            )
        elif writer == "commit_composition_response":
            await service.commit_composition_response(
                session_id=session.id,
                expected_current_state_id=first.id,
                state=_state(),
                assistant_content="done",
                raw_content=None,
                session_operation_context=lease.context,
            )
        else:
            # The guided settlement needs a claimed guided pair under the same
            # COMPOSE context (service.py:5280, :5300-5306) and fences the write
            # to the observed head, so both expected_* name ``first``
            # (service.py:10619-10652; ``(None, None)`` would conflict here).
            claimed = await service.reserve_guided_operation(
                session_id=session.id,
                operation_id="i3-supersede-guided",
                kind="guided_convert",
                request_hash="a" * 64,
                actor="route",
                lease_seconds=60,
                session_operation_context=lease.context,
            )
            assert isinstance(claimed, GuidedOperationClaimed)
            await service.save_state_for_guided_operation(
                claimed.fence,
                expected_current_state_id=first.id,
                expected_current_state_version=first.version,
                # The state shape test_guided_operation_revert_service.py:1650 settles green.
                state=CompositionStateData(composer_meta={"guided_session": {"schema_version": 9}}, is_valid=True),
                provenance="post_compose",
                actor="route",
                response_hash_factory=lambda record: stable_hash({"state_id": str(record.id)}),
                session_operation_context=lease.context,
            )
    decision, decided_at = _decision(engine, open_id)
    assert decision == "superseded"
    assert decided_at is not None
    assert _decision(engine, other_open) == (None, None), "another session's open request is untouched"


@pytest.mark.parametrize(
    "writer",
    [
        # Objects, not attribute names: getattr(owner, "name") is a masquerade-gate probe.
        pytest.param(repository_module._RepositoryCompositionStateMutations.append_state, id="append_state"),
        pytest.param(repository_module._RepositoryInterpretationMutations.create_or_reconcile_pending, id="create_or_reconcile_pending"),
        pytest.param(service_module.SessionServiceImpl._insert_composition_state, id="_insert_composition_state"),
    ],
)
def test_every_composition_state_insert_site_calls_supersede_open_approvals(writer: Any) -> None:
    """Structural pin: the retirement is in the SAME function as the INSERT, after it (same-transaction rule)."""
    source = textwrap.dedent(inspect.getsource(writer))
    insert_at = source.index("insert(composition_states_table)")
    assert "supersede_open_approvals(" in source, f"{writer.__name__} inserts a composition state without retiring open approvals"
    assert source.index("supersede_open_approvals(") > insert_at


def test_fork_child_state_is_the_documented_exclusion() -> None:
    """``insert_child_state`` writes the FIRST state of a NEW session; no approval can name that session yet."""
    source = textwrap.dedent(inspect.getsource(repository_module._ForkChildSessionMutations.insert_child_state))
    assert "insert(composition_states_table)" in source
    assert "supersede_open_approvals(" not in source
    assert "no approval can name" in source
```

- [ ] **Step 18: Run the supersede tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_state_writers_supersede_approvals.py -n 0 > /tmp/i3-lane-supersede-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; the four behavioural cases (`save_composition_state`, `save_composition_state_with_interpretations`, `commit_composition_response`, `save_state_for_guided_operation`) fail with `assert None == 'superseded'`, the three structural cases with `AssertionError: append_state inserts a composition state without retiring open approvals` (and the two siblings), and the fork exclusion fails on `"no approval can name"`.

- [ ] **Step 19: Hook every composition-state head writer.**

1. `src/elspeth/web/coordination/repository.py`, `append_state` (:793): directly after the `supersede_dead_site_pending_interpretation_events(...)` call (:881-886) and before `return record`:

```python
        # Spec :1416: any new state_id marks the open approval request
        # superseded, in THIS transaction, by the only writer of that value.
        from elspeth.web.coordination.approval_authority import supersede_open_approvals

        supersede_open_approvals(connection, session_id=state._session_id, now=state._database_now)
```

2. Same file, `create_or_reconcile_pending`'s appended-head arm (:1240-1290): directly after its `supersede_dead_site_pending_interpretation_events(...)` call (after :1282) and inside the same `if` block:

```python
            from elspeth.web.coordination.approval_authority import supersede_open_approvals

            supersede_open_approvals(connection, session_id=state._session_id, now=state._database_now)
```

3. Same file, `_ForkChildSessionMutations.insert_child_state` (:3877): extend the docstring-less comment above `assert_guided_custody_persistable` (:3887) with the exclusion the structural test pins:

```python
        # Approval supersession is deliberately NOT called here: this is the
        # FIRST state of a NEW session and no approval can name that session
        # yet (approvals.session_id FK), so the sweep would be a no-op read.
```

4. `src/elspeth/web/sessions/service.py`, `_insert_composition_state` (:6260): directly after the `self._supersede_dead_site_pending_interpretation_events(...)` call (:6422-6426) and before `return allocated_state_id`:

```python
        # Spec :1416: any new state_id marks the open approval request
        # superseded. Same connection, same transaction, same rule as the
        # dead-site sweep above; ``append_state`` runs the identical call.
        from elspeth.web.coordination.approval_authority import supersede_open_approvals

        supersede_open_approvals(conn, session_id=session_id, now=created_at if created_at is not None else datetime.now(UTC))
```

- [ ] **Step 20: Run the supersede tests, then re-fingerprint the three edited writers in the manifest.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_state_writers_supersede_approvals.py tests/unit/web/sessions/test_composition_states.py tests/unit/web/coordination/test_session_mutation_facets.py tests/unit/web/coordination/test_session_derived_mutations.py -n 0 > /tmp/i3-lane-supersede-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Then: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -rx > /tmp/i3-lane-manifest-5.log 2>&1; echo exit=$?`
Expected: XFAIL naming as unexpected the re-fingerprinted `composition_states insert` rows of `SessionServiceImpl._insert_composition_state` (old `fp=abb7447bf7be0537`, :2203-2212), `_RepositoryCompositionStateMutations.append_state` (old `fp=8290255ca5f496f1`, :2213-2222) and `_RepositoryInterpretationMutations.create_or_reconcile_pending` (old `fp=4e9d29a674a21658` for the composition_states insert at :2278-2287 — its interpretation_events rows share the function so they re-fingerprint too, :2248-2277), and the same rows as stale. Replace each stale row's fingerprint and `line=` with the printed values; nothing else in those rows changes. Re-run it with its log path changed to `/tmp/i3-lane-manifest-6.log`; expected `exit=0` and `1 xfailed`: the gate already XFAILs on a clean HEAD (measured 2026-09-14 on a `git archive` export of 818d04577, `1 xfailed in 115.45s`), so this task cannot bring it to a pass. Read the XFAIL text instead: its counts must equal I1 Step 15's recorded baseline, and no `Unexpected/unreviewed` or `Stale reviewed` row may name a site this task touches (`grep -c '_assess' /tmp/i3-lane-manifest-6.log` prints `0`).

- [ ] **Step 21: Write the failing execution-service wiring tests.**

Create `tests/unit/web/execution/test_approval_gate_wiring.py`:

```python
"""The execution service compiles ONE binding and hands the permit path the gate only under governance."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import pytest

from elspeth.web.execution import service as service_module
from elspeth.web.execution.service import ExecutionServiceImpl


def test_execute_and_request_compile_the_binding_through_one_helper() -> None:
    """Both paths call ``_prepare_run_settings``; neither hashes a config the other did not build."""
    execute = textwrap.dedent(inspect.getsource(ExecutionServiceImpl.execute_pipeline))
    compile_ = textwrap.dedent(inspect.getsource(ExecutionServiceImpl.compile_approval_binding))
    assert execute.count("self._prepare_run_settings(") == 1
    assert compile_.count("self._prepare_run_settings(") == 1
    assert "validate_plugin_policy(" not in execute and "validate_plugin_policy(" not in compile_
    assert "_approval_binding_for(" in execute and "_approval_binding_for(" in compile_


def test_execute_pipeline_gates_before_create_run() -> None:
    execute = textwrap.dedent(inspect.getsource(ExecutionServiceImpl.execute_pipeline))
    assert execute.index("evaluate_approval_gate(") < execute.index("self._session_service.create_run(")
    assert 'self._settings.workflow_governance == "on"' in execute


@pytest.mark.parametrize(
    "site",
    [
        # Objects, not attribute names: getattr(cls, "name") is a masquerade-gate probe.
        pytest.param(ExecutionServiceImpl.recover_run, id="recover_run"),
        pytest.param(ExecutionServiceImpl._run_pipeline, id="_run_pipeline"),
    ],
)
def test_every_permit_call_site_passes_the_gate_inputs(site: Any) -> None:
    source = textwrap.dedent(inspect.getsource(site))
    for method in ("assess_run_start_admission(", "issue_run_start_permit("):
        for start in [i for i in range(len(source)) if source.startswith(method, i)]:
            call = source[start : source.index(")", start) + 1]
            assert "approval=" in call, f"{site.__name__}: {call} does not pass the approval gate inputs"


@pytest.mark.asyncio
async def test_gate_inputs_are_none_when_governance_is_off_and_derived_from_the_envelope_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.contracts.hashing import CANONICAL_VERSION, stable_hash
    from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence

    evidence = WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="a" * 64,
        snapshot_hash="b" * 64,
        authorized_plugin_ids=("sink:csv", "source:csv"),
        available_plugin_ids=("sink:csv", "source:csv"),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(),
        binding_generation_fingerprint="c" * 64,
        decision_codes=("policy_allowed",),
    )
    envelope = SimpleNamespace(audit_safe_config={"pipeline": {"name": "p"}}, openrouter_catalog_sha256="2" * 64, web_plugin_policy_evidence=evidence)
    monkeypatch.setattr(service_module, "read_cancelled_execution_envelope", lambda _input: envelope)
    monkeypatch.setattr(service_module, "runtime_val_manifest_sha256", lambda: "3" * 64)

    async def get_run_execution_input(_run_id: Any) -> Any:
        return object()

    for governance, expected in (("off", None), ("on", "inputs")):
        svc = ExecutionServiceImpl.__new__(ExecutionServiceImpl)
        svc._settings = SimpleNamespace(workflow_governance=governance)
        svc._session_service = SimpleNamespace(get_run_execution_input=get_run_execution_input)
        inputs = await svc._approval_gate_inputs_for_run(uuid4())
        if expected is None:
            assert inputs is None
        else:
            assert inputs is not None
            assert inputs.evidence is evidence
            assert inputs.config_hash == stable_hash(deep_thaw({"pipeline": {"name": "p"}}))
            assert inputs.canonical_version == CANONICAL_VERSION
            assert inputs.openrouter_catalog_sha256 == "2" * 64
            assert inputs.runtime_val_manifest_sha256 == "3" * 64
```

- [ ] **Step 22: Run the wiring tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/execution/test_approval_gate_wiring.py -n 0 > /tmp/i3-lane-wiring-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; `AttributeError: type object 'ExecutionServiceImpl' has no attribute 'compile_approval_binding'` (and `_approval_gate_inputs_for_run`), the `_run_pipeline` / `recover_run` cases fail on `approval=`.

- [ ] **Step 23: Extract `_prepare_run_settings`, add `compile_approval_binding`, the gate inputs and the pre-flight.**

In `src/elspeth/web/execution/service.py`:

1. Module imports: add
   `from elspeth.web.coordination.approval_authority import ApprovalBinding, ApprovalGateInputs, ApprovalTransactionAuthority, build_approval_binding, evaluate_approval_gate, runtime_val_manifest_sha256`
   and `from elspeth.web.execution.envelope import read_cancelled_execution_envelope` if `read_cancelled_execution_envelope` is not already a module-level import (:2066 imports it lazily inside `_materialize_durable_cancellation`; lift it to the module so the wiring test's monkeypatch and the new helper share one name), and `from elspeth.contracts.hashing import CANONICAL_VERSION, stable_hash` (`stable_hash` is already imported at the top; add `CANONICAL_VERSION` beside it and delete the two lazy `from elspeth.contracts.hashing import CANONICAL_VERSION` lines at :2065 and :2102's block).

2. Add, above `class ExecutionServiceImpl`, the exception and the prepared-settings record:

```python
class ExecutionApprovalRequired(Exception):
    """R2 at the HTTP boundary: the compiled binding has no approved match (sso-design.md:1060)."""

    def __init__(self, reason: AdmissionRefusalReason, binding: ApprovalBinding) -> None:
        self.reason = reason
        self.binding = binding
        super().__init__(f"Run admission refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class _PreparedRunSettings:
    """Everything ``execute_pipeline`` derived from the state before it touched storage."""

    plugin_snapshot: PluginAvailabilitySnapshot
    policy_result: Any
    pipeline_yaml: str
    executable_pipeline_yaml: str
    frozen_run_settings: FrozenRunSettings
```

   (`AdmissionRefusalReason` is imported from `elspeth.contracts.chargeable_admission`; add it to that import line.)

3. Cut `execute_pipeline` :1597-1815 — from `plugin_snapshot = self._plugin_snapshot_for_user(user_id, operation="execution")` through the `FrozenRunSettings(...)` construction (`:1813-1815`, ending at the closing `)`) — into a new method placed directly above `execute_pipeline`:

```python
    async def _prepare_run_settings(
        self,
        composition_state: CompositionState,
        *,
        authored_state: CompositionState,
        session_id: UUID,
        user_id: str | None,
        session_operation_context: SessionOperationContext,
        session_operation_lease: SessionOperationLease | None,
        completion_gates: CompletionGateFacts | None,
        fanout_ack_token: str | None,
        secret_ack_token: str | None,
        enforce_ack_tokens: bool,
    ) -> _PreparedRunSettings:
        """Preflight, policy-lower, path-resolve and guard-annotate one state; no storage side effect.

        Shared by ``execute_pipeline`` and ``compile_approval_binding`` so the
        ``audit_safe_config`` the binding hashes is byte-identical to the one
        the envelope persists (Task I3 decision 2). ``enforce_ack_tokens=False``
        (the approval-request path) applies the secret and fanout guard
        annotations without demanding the acknowledgement tokens: the tokens
        are a run-time consent, the annotations are deterministic in the
        state, and the binding must not depend on whether consent was given.
        """
```

   The body is the moved block verbatim, with three mechanical edits: every
   `await run_sync_in_worker(session_operation_lease.guard_external_effect)`
   becomes `if session_operation_lease is not None: await run_sync_in_worker(session_operation_lease.guard_external_effect)`;
   the two ack-token arms (`:1794-1795` and `:1804-1805` on HEAD) become

```python
        if secret_guard is not None:
            if enforce_ack_tokens and secret_ack_token != secret_guard.ack_token:
                raise ExecutionSecretApprovalRequired(secret_guard)
            pipeline_yaml = annotate_pipeline_yaml_with_secret_guard(pipeline_yaml, secret_guard)
```

```python
        if fanout_guard is not None:
            if enforce_ack_tokens and fanout_ack_token != fanout_guard.ack_token:
                raise ExecutionFanoutGuardRequired(fanout_guard)
            pipeline_yaml = annotate_pipeline_yaml_with_fanout_guard(pipeline_yaml, fanout_guard)
```

   and the method ends with
   `return _PreparedRunSettings(plugin_snapshot=plugin_snapshot, policy_result=policy_result, pipeline_yaml=pipeline_yaml, executable_pipeline_yaml=executable_pipeline_yaml, frozen_run_settings=frozen_run_settings)`.
   `execute_pipeline` then reads:

```python
        prepared = await self._prepare_run_settings(
            composition_state,
            authored_state=authored_state,
            session_id=session_id,
            user_id=user_id,
            session_operation_context=session_operation_context,
            session_operation_lease=session_operation_lease,
            completion_gates=completion_gates,
            fanout_ack_token=fanout_ack_token,
            secret_ack_token=secret_ack_token,
            enforce_ack_tokens=True,
        )
        plugin_snapshot = prepared.plugin_snapshot
        policy_result = prepared.policy_result
        pipeline_yaml = prepared.pipeline_yaml
        executable_pipeline_yaml = prepared.executable_pipeline_yaml
        frozen_run_settings = prepared.frozen_run_settings
```

   followed by the unchanged `retain_execution_inputs` line (:1817) onward.
   Re-run `pytest tests/unit/web/execution/test_service.py -n 0` after this
   move alone (log `/tmp/i3-lane-extract.log`, expected `exit=0`) BEFORE adding
   the gate: a red here is the extraction, not R2.

4. Add the binding compiler and the gate-input reader, directly below `_prepare_run_settings`:

```python
    def _approval_binding_for(self, prepared: _PreparedRunSettings) -> ApprovalBinding:
        """The compiled binding for one prepared state (Task I3 decision 2)."""
        if self._openrouter_catalog_sha256 is None:
            raise RuntimeError("OpenRouter catalog snapshot must be set before compiling an approval binding")
        return build_approval_binding(
            evidence=_build_web_plugin_policy_evidence(snapshot=prepared.plugin_snapshot, policy=self._web_plugin_policy),
            config_hash=stable_hash(deep_thaw(prepared.frozen_run_settings.audit_safe_config)),
            canonical_version=CANONICAL_VERSION,
            openrouter_catalog_sha256=self._openrouter_catalog_sha256,
            runtime_val_manifest_sha256=runtime_val_manifest_sha256(),
        )

    async def compile_approval_binding(
        self,
        session_id: UUID,
        state_id: UUID,
        *,
        user_id: str,
        session_operation_context: SessionOperationContext,
    ) -> ApprovalBinding:
        """What an approver approves: the binding ``execute`` will compile for this exact state."""
        state_record = await self._session_service.get_state_in_session(state_id, session_id)
        composition_state = state_from_record(state_record)
        prepared = await self._prepare_run_settings(
            composition_state,
            authored_state=composition_state,
            session_id=session_id,
            user_id=user_id,
            session_operation_context=session_operation_context,
            session_operation_lease=None,
            completion_gates=parse_completion_gates(state_record.composer_meta),
            fanout_ack_token=None,
            secret_ack_token=None,
            enforce_ack_tokens=False,
        )
        return self._approval_binding_for(prepared)

    async def _approval_gate_inputs_for_run(self, run_id: UUID) -> ApprovalGateInputs | None:
        """``None`` under workflow_governance="off"; otherwise the persisted envelope's evidence and shas."""
        if self._settings.workflow_governance != "on":
            return None
        execution_input = await self._session_service.get_run_execution_input(run_id)
        if execution_input is None:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        envelope = read_cancelled_execution_envelope(execution_input)
        if envelope.web_plugin_policy_evidence is None:
            raise RuntimeError("Web execution under workflow governance requires persisted plugin policy evidence")
        return ApprovalGateInputs(
            evidence=envelope.web_plugin_policy_evidence,
            config_hash=stable_hash(deep_thaw(envelope.audit_safe_config)),
            canonical_version=CANONICAL_VERSION,
            openrouter_catalog_sha256=envelope.openrouter_catalog_sha256,
            runtime_val_manifest_sha256=runtime_val_manifest_sha256(),
        )
```

   `get_state_in_session` exists (`sessions/routes/runs.py:83` calls it; it raises `AuditIntegrityError` on a session mismatch, which the route maps to 404 like `StateAccessError`). Import `EnvelopeRecoveryReason` at module level if it is only imported lazily today (:2069).

5. The pre-flight in `execute_pipeline`, directly before `await run_sync_in_worker(session_operation_lease.guard_external_effect)` at :1869 (the line above `create_run`):

```python
        if self._settings.workflow_governance == "on":
            # R2 at the HTTP boundary, through the SAME function the permit path
            # uses in _assess; the durable refusal there stays authoritative.
            if self._approval_authority is None:
                raise RuntimeError("workflow governance requires the approval authority")
            compiled = self._approval_binding_for(prepared)
            # approved_binding opens a sessions connection: keep it off the event
            # loop like every other sync store touch in this method (:1869).
            approved = await run_sync_in_worker(
                self._approval_authority.approved_binding, session_id=str(session_id), state_id=str(state_record.id)
            )
            reason = evaluate_approval_gate(approved=approved, compiled=compiled)
            if reason is not None:
                raise ExecutionApprovalRequired(reason, compiled)
```

   `state_record` is the name `execute_pipeline` already binds (:1411-1425).
   `self._approval_authority` is a new constructor argument. In
   `ExecutionServiceImpl.__init__`, directly after
   `principal_is_active: Callable[[str], bool] | None = None,` (:896) add

```python
        approval_authority: ApprovalTransactionAuthority | None = None,
```

   and directly after `self._principal_is_active = principal_is_active` (:922) add

```python
        # Task I3: the R2 pre-flight reads the approved row through this.
        # ``for_trained_operator`` leaves it None; that composition root never
        # runs with workflow_governance="on", and the pre-flight refuses loudly
        # if it ever does.
        self._approval_authority = approval_authority
```

6. The three permit call sites:
   - `:1932` (`recover_run`): `permit = await self._session_service.assess_run_start_admission(run.id, session_operation_context=session_operation_lease.context, approval=await self._approval_gate_inputs_for_run(run.id))`
   - `:2447` and `:2469` (`_run_pipeline`, sync): insert `approval = self._call_async(self._approval_gate_inputs_for_run(run_uuid))` directly after `from elspeth.web.coordination.contracts import StartPermitState` at :2444, and pass `approval=approval` to both the `assess_run_start_admission(...)` and `issue_run_start_permit(...)` calls.

7. `src/elspeth/web/execution/protocol.py`, in `class ExecutionService(Protocol)` after `validate_state` (:113-137):

```python
    async def compile_approval_binding(
        self,
        session_id: UUID,
        state_id: UUID,
        *,
        user_id: str,
        session_operation_context: SessionOperationContext,
    ) -> ApprovalBinding:
        """The binding ``execute`` will compile for this state; the approval-request route persists it."""
        ...
```

   with `from elspeth.web.coordination.approval_authority import ApprovalBinding` as a module-level import beside `from elspeth.web.coordination.lifecycle import SessionOperationLease` (:18). `execution/protocol.py` has no `TYPE_CHECKING` block; it already imports `elspeth.web.coordination` and `elspeth.web.sessions.protocol` at runtime (:18, :22), so this adds no new import edge.

8. `src/elspeth/web/execution/routes.py`: import `ExecutionApprovalRequired` from `elspeth.web.execution.service` beside the existing service imports, and add directly before `except ExecutionSecretApprovalRequired as exc:` (:1128):

```python
        except ExecutionApprovalRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": exc.reason.value,
                    "detail": str(exc),
                    "binding": exc.binding.as_json(),
                },
            ) from exc
```

9. `src/elspeth/web/app.py`: after `app.state.identity_authority = identity_authority` (:1522) add

```python
    from elspeth.web.coordination.approval_authority import ApprovalTransactionAuthority

    approval_authority = ApprovalTransactionAuthority(session_engine)
    app.state.approval_authority = approval_authority
```

   Then pass the authority to the execution service. `execution_service = ExecutionServiceImpl(` is at `src/elspeth/web/app.py:584` (kwargs :585-597, `app.state.execution_service = execution_service` at :599), inside `lifespan` (:509), while the authority above is built in `create_app` (:1132) — a different function, so the local name is NOT in scope there. Add, directly after `principal_is_active=app.state.principal_is_active,` (:597):

```python
        approval_authority=app.state.approval_authority,
```

   `lifespan` runs after `create_app` has finished, so `app.state.approval_authority` is always set by then.

- [ ] **Step 24: Run the wiring and execution suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/execution -n 0 > /tmp/i3-lane-wiring-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 25: Write the failing approval-route tests.**

Create `tests/unit/web/workflow/__init__.py` (empty) and
`tests/unit/web/workflow/test_approval_routes.py`:

```python
"""Approval routes over a REAL approval authority on the closed local app (I8's ``closed_local_app``).

Two fakes, both deliberate. The audit writer records calls: what these tests
pin is that every mutation hands the authority a record callback that fires
with the record the trail needs (that the Landscape row is written INSIDE the
transaction is pinned against a real Landscape in
tests/integration/web/workflow/test_approvals.py). The execution service
returns a fixed binding: that the request-time binding equals the one
``execute`` compiles is also pinned there, through the real ExecutionServiceImpl.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Request
from sqlalchemy import insert, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_authority import ApprovalBinding, ApprovalTransactionAuthority
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.models import identity_roles_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.workflow.approvals import create_approvals_router

BINDING = ApprovalBinding(
    config_hash="1" * 64,
    canonical_version="sha256-rfc8785-v1",
    runtime_val_manifest_sha256="3" * 64,
    openrouter_catalog_sha256="2" * 64,
    binding_generation_fingerprint="c" * 64,
    policy_hash="a" * 64,
)


@dataclass
class _AuditCall:
    method: str
    request_bound: bool
    kwargs: dict[str, Any]


@dataclass
class _RecordingAuditWriter:
    calls: list[_AuditCall] = field(default_factory=list)

    def _note(self, method: str, request: Request | None, kwargs: dict[str, Any]) -> None:
        self.calls.append(_AuditCall(method=method, request_bound=request is not None, kwargs=kwargs))

    def record_approval_requested(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_approval_requested", request, kwargs)

    def record_approval_decided(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_approval_decided", request, kwargs)

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


@dataclass
class _FakeExecutionService:
    compiled: list[tuple[str, str, str]] = field(default_factory=list)

    async def compile_approval_binding(
        self, session_id: UUID, state_id: UUID, *, user_id: str, session_operation_context: Any
    ) -> ApprovalBinding:
        assert session_operation_context is not None, "the route must compile under a live session-operation lease"
        self.compiled.append((str(session_id), str(state_id), user_id))
        return BINDING


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    """closed_local_app + the approvals router, a real approval authority, a real identity authority and two fakes."""
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("bob", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id, role in (("role-bob", "bob", "approver"), ("role-carol", "carol", "approver"), ("role-dave", "dave", "user")):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=role_id, identity_id=identity_id, role=role, granted_at=now, granted_by_identity_id="alice"
                )
            )
    state = closed_local_app.app.state
    state.approval_authority = ApprovalTransactionAuthority(engine)
    state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    state.auth_audit_recorder = _RecordingAuditWriter()
    state.execution_service = _FakeExecutionService()
    closed_local_app.app.include_router(create_approvals_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def _governance(client: Any, value: str) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": value})


def _save_state(client: Any, session_id: str) -> str:
    """A real head write under a COMPOSE lease, so the supersede hook in append_state runs."""
    service = client.app.state.phase3_sessions_service

    async def save() -> str:
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        async with lease:
            record = await service.save_composition_state(
                UUID(session_id),
                CompositionStateData(sources={}, nodes=[], edges=[], outputs=[], metadata_={}, is_valid=False, validation_errors=None),
                provenance="session_seed",
                session_operation_context=lease.context,
            )
        return str(record.id)

    return asyncio.run(save())


def _session_with_state(client: Any, title: str = "approve me") -> tuple[str, str]:
    _as(client, "alice")
    created = client.post("/api/sessions", json={"title": title})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    return session_id, _save_state(client, session_id)


def _request(client: Any, session_id: str, *, approver: str = "bob", state_id: str | None = None, note: str | None = "please") -> Any:
    _as(client, "alice")
    return client.post(
        f"/api/sessions/{session_id}/approvals", json={"state_id": state_id, "approver_identity_id": approver, "note": note}
    )


def _error(response: Any) -> str:
    return response.json()["detail"]["error_type"]


# ── request ──────────────────────────────────────────────────────────────


def test_request_persists_the_compiled_binding_for_the_current_state_and_records_it(app: Any) -> None:
    session_id, state_id = _session_with_state(app)
    response = _request(app, session_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state_id"] == state_id
    assert body["binding"] == BINDING.as_json()
    assert body["decision"] is None
    assert body["requested_by_identity_id"] == "alice"
    assert body["approver_identity_id"] == "bob"
    assert response.headers["Cache-Control"] == "no-store"
    assert app.app.state.execution_service.compiled == [(session_id, state_id, "alice")]
    (call,) = app.app.state.auth_audit_recorder.calls
    assert call.method == "record_approval_requested"
    assert call.request_bound is True
    assert call.kwargs["provider"] == "local"
    assert call.kwargs["approval"].approval_id == body["approval_id"]


def test_request_on_a_session_the_caller_does_not_own_is_hidden(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    _as(app, "dave")
    response = app.post(f"/api/sessions/{session_id}/approvals", json={"state_id": None, "approver_identity_id": "bob", "note": None})
    assert response.status_code == 404
    assert app.app.state.execution_service.compiled == []
    assert app.app.state.auth_audit_recorder.calls == []


def test_request_naming_a_state_of_another_session_is_404(app: Any) -> None:
    session_id, _state_id = _session_with_state(app, "first")
    _other_session, other_state = _session_with_state(app, "second")
    response = _request(app, session_id, state_id=other_state)
    assert response.status_code == 404
    assert _error(response) == "state_not_found"
    missing = _request(app, session_id, state_id=str(uuid4()))
    assert missing.status_code == 404 and _error(missing) == "state_not_found"
    assert app.app.state.auth_audit_recorder.calls == []


@pytest.mark.parametrize(
    ("approver", "error_type"),
    [("alice", "approval_author_is_approver"), ("dave", "approver_role_required")],
)
def test_request_refusals_are_409_with_the_closed_code(app: Any, approver: str, error_type: str) -> None:
    session_id, _state_id = _session_with_state(app)
    response = _request(app, session_id, approver=approver)
    assert response.status_code == 409
    assert _error(response) == error_type
    assert app.app.state.auth_audit_recorder.calls == []


def test_second_open_request_for_the_same_state_is_409(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    assert _request(app, session_id).status_code == 201
    second = _request(app, session_id, approver="carol")
    assert second.status_code == 409
    assert _error(second) == "approval_open_request_exists"


# ── inbox and sent ───────────────────────────────────────────────────────


def test_inbox_lists_open_requests_addressed_to_a_live_approver_only(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    approval_id = _request(app, session_id).json()["approval_id"]
    _as(app, "bob")
    assert [row["approval_id"] for row in app.get("/api/approvals/inbox").json()["approvals"]] == [approval_id]
    _as(app, "carol")
    assert app.get("/api/approvals/inbox").json() == {"approvals": []}, "addressed to bob, not carol"
    _as(app, "dave")
    assert app.get("/api/approvals/inbox").json() == {"approvals": []}, "no approver role"
    # Mutation-derivation: revoke bob's ROLE ROW (not the route) and his inbox empties.
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "role-bob").values(revoked_at=datetime.now(UTC)))
    _as(app, "bob")
    assert app.get("/api/approvals/inbox").json() == {"approvals": []}


def test_sent_shows_a_new_state_superseding_the_open_request(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    approval_id = _request(app, session_id).json()["approval_id"]
    _save_state(app, session_id)
    _as(app, "alice")
    (row,) = app.get("/api/approvals/sent").json()["approvals"]
    assert row["approval_id"] == approval_id
    assert row["decision"] == "superseded"
    assert row["decided_at"] is not None
    assert app.app.state.auth_audit_recorder.methods() == ["record_approval_requested"], "superseded has no Landscape row"


# ── decide and withdraw ──────────────────────────────────────────────────


def test_any_live_approver_decides_once_and_the_second_decider_gets_the_current_state(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    approval_id = _request(app, session_id).json()["approval_id"]
    _as(app, "carol")
    decided = app.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved", "note": None})
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "approved"
    assert decided.json()["decided_by_identity_id"] == "carol"
    call = app.app.state.auth_audit_recorder.calls[-1]
    assert call.method == "record_approval_decided"
    assert call.kwargs["actor_identity_id"] == "carol"
    _as(app, "bob")
    again = app.post(f"/api/approvals/{approval_id}/decide", json={"decision": "rejected", "note": "too late"})
    assert again.status_code == 409
    assert again.json()["detail"] == {
        "error_type": "approval_already_decided",
        "detail": "approval is already approved",
        "current_state": "approved",
    }
    assert app.app.state.auth_audit_recorder.methods() == ["record_approval_requested", "record_approval_decided"]


def test_reject_without_a_note_is_409(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    approval_id = _request(app, session_id).json()["approval_id"]
    _as(app, "bob")
    response = app.post(f"/api/approvals/{approval_id}/decide", json={"decision": "rejected", "note": None})
    assert response.status_code == 409
    assert _error(response) == "approval_note_required"


def test_decide_and_withdraw_on_an_unknown_approval_are_404(app: Any) -> None:
    _as(app, "bob")
    decide = app.post("/api/approvals/no-such-approval/decide", json={"decision": "approved", "note": None})
    assert decide.status_code == 404 and _error(decide) == "approval_not_found"
    _as(app, "alice")
    withdraw = app.post("/api/approvals/no-such-approval/withdraw")
    assert withdraw.status_code == 404 and _error(withdraw) == "approval_not_found"


def test_withdraw_is_requester_only_and_records_revoked(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    approval_id = _request(app, session_id).json()["approval_id"]
    _as(app, "bob")
    refused = app.post(f"/api/approvals/{approval_id}/withdraw")
    assert refused.status_code == 409
    assert _error(refused) == "approval_withdraw_requires_requester"
    _as(app, "alice")
    withdrawn = app.post(f"/api/approvals/{approval_id}/withdraw")
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["decision"] == "revoked"
    assert withdrawn.json()["revocation_actor_kind"] == "identity"
    assert withdrawn.json()["revoked_by_identity_id"] == "alice"
    call = app.app.state.auth_audit_recorder.calls[-1]
    assert call.method == "record_approval_decided"
    assert call.kwargs["actor_identity_id"] == "alice"


# ── governance switch ────────────────────────────────────────────────────


def test_governance_off_refuses_every_mutation_and_empties_both_lists(app: Any) -> None:
    session_id, _state_id = _session_with_state(app)
    _governance(app, "off")
    refused = _request(app, session_id)
    assert refused.status_code == 409 and _error(refused) == "workflow_governance_off"
    assert _error(app.post("/api/approvals/any/decide", json={"decision": "approved", "note": None})) == "workflow_governance_off"
    assert _error(app.post("/api/approvals/any/withdraw")) == "workflow_governance_off"
    assert app.get("/api/approvals/sent").json() == {"approvals": []}
    _as(app, "bob")
    assert app.get("/api/approvals/inbox").json() == {"approvals": []}
    assert app.app.state.execution_service.compiled == []
    # Mutation-derivation: the switch, not the fixture, admits the request.
    _governance(app, "on")
    assert _request(app, session_id).status_code == 201
    assert app.app.state.auth_audit_recorder.methods() == ["record_approval_requested"]
```

- [ ] **Step 26: Run the route tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_approval_routes.py -n 0 > /tmp/i3-lane-routes-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (collection error) with `ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow'`.

- [ ] **Step 27: Create the router package, the approvals router, and register it.**

Create `src/elspeth/web/sessions/routes/workflow/__init__.py`:

```python
"""Workflow-governance routers (sso-design.md R2, R8).

One module per product, each exposing its own ``create_<product>_router()``
that ``web/app.py`` registers: ``approvals.py`` (Task I3), ``reviews.py``
(Task I4), ``library.py`` (Task I5). This package marker carries no code, so
two lanes adding sibling modules never edit the same file here.
"""
```

Create `src/elspeth/web/sessions/routes/workflow/approvals.py`:

```python
"""Approval requests and decisions -- the I3 half of the workflow mailbox.

``POST /api/sessions/{session_id}/approvals`` (requester = session owner),
``GET /api/approvals/inbox`` (approvers), ``GET /api/approvals/sent``
(requesters), ``POST /api/approvals/{approval_id}/decide`` (approvers),
``POST /api/approvals/{approval_id}/withdraw`` (requester).

The authority is the arbiter: role, activity, author-is-not-approver,
one-open-request, the note rules and the lock order are enforced inside
``RepositoryApprovalAuthority`` under ``ApprovalTransactionAuthority.run``'s
session lock. These routes parse the body, prove session ownership for the
request route, compile the binding through the execution service (the SAME
compiler ``execute`` uses, so what an approver approves is what R2 checks),
hand the authority a record callback and translate its closed refusal set.
They add one rule of their own: every mutation refuses with
``workflow_governance_off`` unless ``WebSettings.workflow_governance`` is
``"on"`` (Task I8), and both lists are empty while it is off.

The binding is COMPUTED HERE from the state, never accepted from the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_authority import (
    MAX_APPROVAL_NOTE_BYTES,
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalRefusal,
    ApprovalTransactionAuthority,
    RepositoryApprovalAuthority,
)
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.protocol import ExecutionService
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _verify_session_ownership

# ── wire shapes ──────────────────────────────────────────────────────────


class ApprovalRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_id: UUID | None = None
    approver_identity_id: str = Field(min_length=1, max_length=64)
    # Characters here; the authority bounds BYTES (MAX_APPROVAL_NOTE_BYTES).
    note: str | None = Field(default=None, max_length=MAX_APPROVAL_NOTE_BYTES)


class ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=MAX_APPROVAL_NOTE_BYTES)


class ApprovalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    session_id: str
    state_id: str
    binding: dict[str, str]
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None
    decision: Literal["approved", "rejected", "revoked", "superseded"] | None
    request_note: str | None
    decision_seen_at: AwareDatetime | None
    decided_by_identity_id: str | None
    decision_note: str | None
    revoked_by_identity_id: str | None
    revocation_actor_kind: str | None
    revocation_event_id: str | None


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    approvals: list[ApprovalView]


# ── helpers ──────────────────────────────────────────────────────────────


def _authority(request: Request) -> ApprovalTransactionAuthority:
    authority: ApprovalTransactionAuthority = request.app.state.approval_authority
    return authority


def _identity_authority(request: Request) -> RepositoryIdentityAuthority:
    authority: RepositoryIdentityAuthority = request.app.state.identity_authority
    return authority


def _session_service(request: Request) -> SessionServiceProtocol:
    service: SessionServiceProtocol = request.app.state.session_service
    return service


def _execution_service(request: Request) -> ExecutionService:
    service: ExecutionService = request.app.state.execution_service
    return service


def _recorder(request: Request) -> AuthAuditWriter:
    recorder: AuthAuditWriter = request.app.state.auth_audit_recorder
    return recorder


def _settings(request: Request) -> WebSettings:
    settings: WebSettings = request.app.state.settings
    return settings


def _provider(request: Request) -> AuthProviderType:
    return _settings(request).auth_provider


def _governance_on(request: Request) -> bool:
    return _settings(request).workflow_governance == "on"


def _require_governance(request: Request) -> None:
    if not _governance_on(request):
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": "workflow_governance_off",
                "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)",
            },
        )


def _refusal_code(exc: ApprovalRefusal) -> str:
    """``ApproverRoleRequired`` -> ``approver_role_required``: the closed code a client can switch on."""
    name = type(exc).__name__
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name).lstrip("_")


def _refused(exc: ApprovalRefusal) -> HTTPException:
    """Exact type for the one not-found arm: a new refusal class lands in the 409 arm by default."""
    if type(exc) is ApprovalNotFound:
        return HTTPException(status_code=404, detail={"error_type": _refusal_code(exc), "detail": str(exc)})
    if isinstance(exc, ApprovalAlreadyDecided):
        return HTTPException(
            status_code=409,
            detail={"error_type": _refusal_code(exc), "detail": str(exc), "current_state": exc.current_state},
        )
    return HTTPException(status_code=409, detail={"error_type": _refusal_code(exc), "detail": str(exc)})


def _state_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error_type": "state_not_found", "detail": "State not found"})


def _uncacheable(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _view(record: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=record.approval_id,
        session_id=record.session_id,
        state_id=record.state_id,
        binding=record.binding.as_json(),
        requested_by_identity_id=record.requested_by_identity_id,
        approver_identity_id=record.approver_identity_id,
        requested_at=record.requested_at,
        decided_at=record.decided_at,
        decision=record.decision,
        request_note=record.request_note,
        decision_seen_at=record.decision_seen_at,
        decided_by_identity_id=record.decided_by_identity_id,
        decision_note=record.decision_note,
        revoked_by_identity_id=record.revoked_by_identity_id,
        revocation_actor_kind=record.revocation_actor_kind,
        revocation_event_id=record.revocation_event_id,
    )


# ── router ───────────────────────────────────────────────────────────────


def create_approvals_router() -> APIRouter:
    router = APIRouter(tags=["workflow-approvals"])

    @router.post("/api/sessions/{session_id}/approvals", response_model=ApprovalView, status_code=201)
    async def request_approval(
        session_id: UUID,
        request: Request,
        response: Response,
        body: ApprovalRequestBody,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalView:
        _require_governance(request)
        # 404 on a session the caller does not own (IDOR rule, _helpers.py:2527).
        session = await _verify_session_ownership(session_id, user, request)
        session_service = _session_service(request)
        if body.state_id is None:
            current = await session_service.get_current_state(session_id)
            if current is None:
                raise _state_not_found()
            state_id = current.id
        else:
            try:
                state_record = await session_service.get_state(body.state_id)
            except ValueError as exc:
                raise _state_not_found() from exc
            if state_record.session_id != session_id:
                raise _state_not_found()
            state_id = state_record.id
        # The binding is compiled under a BLOB_READ lease exactly as the
        # validate route reads state (execution/routes.py:955-961).
        lease = await SessionOperationLease.acquire(
            session_service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id=session_service.session_operation_owner_instance_id,
            lease_seconds=session_service.session_operation_lease_seconds,
        )
        try:
            binding = await _execution_service(request).compile_approval_binding(
                session_id, state_id, user_id=user.user_id, session_operation_context=lease.context
            )
        finally:
            await lease.close()
        recorder = _recorder(request)
        provider = _provider(request)

        def record(created: ApprovalRecord) -> None:
            recorder.record_approval_requested(request, provider=provider, approval=created)

        def mutation(token: str, now: datetime) -> ApprovalRecord:
            return RepositoryApprovalAuthority.request(
                token,
                session_id=str(session.id),
                state_id=str(state_id),
                binding=binding,
                requested_by=user.user_id,
                approver=body.approver_identity_id,
                note=body.note,
                now=now,
                record=record,
            )

        try:
            created = await run_sync_in_worker(_authority(request).run, str(session.id), mutation)
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(created)

    @router.get("/api/approvals/inbox", response_model=ApprovalListResponse)
    async def approval_inbox(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalListResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApprovalListResponse(approvals=[])
        # Checked against the store per request: a revoked approver sees nothing.
        if not await run_sync_in_worker(_identity_authority(request).holds_active_role, identity_id=user.user_id, role="approver"):
            return ApprovalListResponse(approvals=[])
        rows = await run_sync_in_worker(_authority(request).inbox, approver_identity_id=user.user_id)
        return ApprovalListResponse(approvals=[_view(row) for row in rows])

    @router.get("/api/approvals/sent", response_model=ApprovalListResponse)
    async def approvals_sent(
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalListResponse:
        _uncacheable(response)
        if not _governance_on(request):
            return ApprovalListResponse(approvals=[])
        rows = await run_sync_in_worker(_authority(request).sent, requested_by_identity_id=user.user_id)
        return ApprovalListResponse(approvals=[_view(row) for row in rows])

    @router.post("/api/approvals/{approval_id}/decide", response_model=ApprovalView)
    async def decide_approval(
        approval_id: str,
        request: Request,
        response: Response,
        body: ApprovalDecisionBody,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalView:
        _require_governance(request)
        authority = _authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _refused(ApprovalNotFound(approval_id))
        recorder = _recorder(request)
        provider = _provider(request)

        def record(decided: ApprovalRecord) -> None:
            recorder.record_approval_decided(request, provider=provider, approval=decided, actor_identity_id=user.user_id)

        def mutation(token: str, now: datetime) -> ApprovalRecord:
            return RepositoryApprovalAuthority.decide(
                token,
                approval_id=approval_id,
                decided_by=user.user_id,
                decision=body.decision,
                note=body.note,
                now=now,
                record=record,
            )

        try:
            decided = await run_sync_in_worker(authority.run, session_id, mutation)
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(decided)

    @router.post("/api/approvals/{approval_id}/withdraw", response_model=ApprovalView)
    async def withdraw_approval(
        approval_id: str,
        request: Request,
        response: Response,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> ApprovalView:
        _require_governance(request)
        authority = _authority(request)
        session_id = await run_sync_in_worker(authority.session_id_of, approval_id)
        if session_id is None:
            raise _refused(ApprovalNotFound(approval_id))
        recorder = _recorder(request)
        provider = _provider(request)

        def record(withdrawn: ApprovalRecord) -> None:
            recorder.record_approval_decided(request, provider=provider, approval=withdrawn, actor_identity_id=user.user_id)

        def mutation(token: str, now: datetime) -> ApprovalRecord:
            return RepositoryApprovalAuthority.withdraw(
                token, approval_id=approval_id, requested_by=user.user_id, now=now, record=record
            )

        try:
            withdrawn = await run_sync_in_worker(authority.run, session_id, mutation)
        except ApprovalRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _view(withdrawn)

    return router
```

The `# noqa: B008` on each `Depends(...)` default is the FastAPI house form
already used on every route in `auth/identity_admin_routes.py` (e.g. :496) and
I4's `reviews.py`; it is not a new suppression kind.

In `src/elspeth/web/app.py`, add
`from elspeth.web.sessions.routes.workflow.approvals import create_approvals_router`
beside `from elspeth.web.shareable_reviews.routes import create_shareable_reviews_router`
(:164), and directly after `app.include_router(create_identity_admin_router())`
(:1782) add:

```python
    app.include_router(create_approvals_router())
```

- [ ] **Step 28: Run the route tests and the app-wiring suite to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_approval_routes.py tests/unit/web/test_app.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i3-lane-routes-green.log 2>&1; echo exit=$?`
Expected: `exit=0`; `test_approval_routes.py` contributes `13 passed` (12 test functions, `test_request_refusals_are_409_with_the_closed_code` parametrised twice).

- [ ] **Step 29: Write the integration test: R2 through the real binding tuple, the Landscape rows, and the R4 rollback.**

Create `tests/integration/web/workflow/__init__.py` (empty) and
`tests/integration/web/workflow/test_approvals.py`:

```python
"""Approvals end to end through ``create_app`` with workflow governance on.

What only this module proves:

* the binding the approval-request route compiles is byte-equal to the one
  ``execute`` compiles for the same state (the 409's ``binding`` == the
  request's ``binding``), and the permit path's envelope-derived binding
  equals both (an approved run reaches ``completed``; a mismatch would
  refuse it with ``approval_binding_mismatch``);
* ``approval_requested`` / ``approval_decided`` rows land in the LANDSCAPE
  ``auth_events`` table (read here from the Landscape database, not the
  sessions database), and ``superseded`` writes none;
* a failed audit write rolls the approval row back (R4).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _save_composition_state_with_compose_authority

from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.coordination.approval_authority import ApprovalTransactionAuthority, RepositoryApprovalAuthority, build_approval_binding
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approvals_table, identity_roles_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
TEST_CSV = FIXTURES_DIR / "test_input.csv"


def _state_data(work_dir: Path, session_id: str) -> Any:
    from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
    from elspeth.web.sessions.protocol import CompositionStateData

    session_blob_dir = work_dir / "blobs" / session_id
    session_blob_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEST_CSV, session_blob_dir / "input.csv")
    schema = {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]}
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(session_blob_dir / "input.csv"), "schema": schema},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="primary",
                plugin="csv",
                options={"path": str(work_dir / "outputs" / session_id / "result.csv"), "schema": schema},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Approval integration", description="CSV passthrough under workflow governance"),
        version=1,
    )
    state_d = state.to_dict()
    return CompositionStateData(
        sources=state_d["sources"],
        nodes=state_d["nodes"],
        edges=state_d["edges"],
        outputs=state_d["outputs"],
        metadata_=state_d["metadata"],
        is_valid=True,
        validation_errors=None,
    )


def _auth_event_types(landscape_url: str) -> list[str]:
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import auth_events_table

    with LandscapeDB.from_url(landscape_url) as db, db.read_only_connection() as conn:
        rows = conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall()
    return [row.event_type for row in rows if row.event_type.startswith("approval_")]


async def _login(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, f"login {username} failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _wait_for_terminal(client: AsyncClient, run_id: str, headers: dict[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        status: dict[str, Any] = response.json()
        if status["status"] in ("completed", "failed", "cancelled"):
            return status
        await asyncio.sleep(0.5)
    pytest.fail("pipeline did not reach a terminal status within 30 seconds")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r2_through_the_real_binding_tuple(tmp_path: Path) -> None:
    from asgi_lifespan import LifespanManager

    from elspeth.web.app import create_app
    from elspeth.web.config import WebSettings

    for directory in ("blobs", "outputs", "runs"):
        (tmp_path / directory).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    landscape_url = f"sqlite:///{tmp_path}/runs/audit.db"
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=landscape_url,
        payload_store_path=tmp_path / "payloads",
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="test-compartment",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app = create_app(settings=settings)
    # Closed registration: a first login is refused unless the identity is
    # already active (auth/local.py:1159-1167), so both people are seeded
    # active under their usernames (local subject == username, local.py:1126).
    now = datetime.now(UTC)
    with app.state.session_engine.begin() as conn:
        for identity_id in ("alice", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="role-bob-approver", identity_id="bob", role="approver", granted_at=now, granted_by_identity_id="alice"
            )
        )
    app.state.auth_provider.create_user("alice", "alicepass123", display_name="Alice")
    app.state.auth_provider.create_user("bob", "bobpass1234", display_name="Bob")

    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        alice = await _login(client, "alice", "alicepass123")
        bob = await _login(client, "bob", "bobpass1234")
        created = await client.post("/api/sessions", headers=alice, json={"title": "governed"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        session_service = app.state.session_service
        await _save_composition_state_with_compose_authority(
            session_service, UUID(session_id), _state_data(tmp_path, session_id), provenance="session_seed"
        )

        # 1. No approval: the pre-flight refuses before create_run, carrying the compiled binding.
        refused = await client.post(f"/api/sessions/{session_id}/execute", headers=alice)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["error_type"] == "approval_required"
        execute_binding = refused.json()["detail"]["binding"]
        assert set(execute_binding) == {
            "config_hash",
            "canonical_version",
            "runtime_val_manifest_sha256",
            "openrouter_catalog_sha256",
            "binding_generation_fingerprint",
            "policy_hash",
        }

        # 2. The request route compiles the SAME tuple for the same state.
        requested = await client.post(
            f"/api/sessions/{session_id}/approvals", headers=alice, json={"state_id": None, "approver_identity_id": "bob", "note": "ship it"}
        )
        assert requested.status_code == 201, requested.text
        assert requested.json()["binding"] == execute_binding
        approval_id = requested.json()["approval_id"]

        # 3. Approve, execute, and the permit path (envelope-derived binding) lets the run complete.
        decided = await client.post(f"/api/approvals/{approval_id}/decide", headers=bob, json={"decision": "approved", "note": None})
        assert decided.status_code == 200, decided.text
        started = await client.post(f"/api/sessions/{session_id}/execute", headers=alice)
        assert started.status_code == 202, started.text
        status = await _wait_for_terminal(client, started.json()["run_id"], alice)
        assert status["status"] == "completed", status

        # 4. A new state leaves no approval for the head: an open request on
        #    state 2 is superseded by state 3, and execute refuses again.
        await _save_composition_state_with_compose_authority(
            session_service, UUID(session_id), _state_data(tmp_path, session_id), provenance="session_seed"
        )
        second = await client.post(
            f"/api/sessions/{session_id}/approvals", headers=alice, json={"state_id": None, "approver_identity_id": "bob", "note": None}
        )
        assert second.status_code == 201, second.text
        await _save_composition_state_with_compose_authority(
            session_service, UUID(session_id), _state_data(tmp_path, session_id), provenance="session_seed"
        )
        sent = (await client.get("/api/approvals/sent", headers=alice)).json()["approvals"]
        assert {row["approval_id"]: row["decision"] for row in sent} == {approval_id: "approved", second.json()["approval_id"]: "superseded"}
        again = await client.post(f"/api/sessions/{session_id}/execute", headers=alice)
        assert again.status_code == 409 and again.json()["detail"]["error_type"] == "approval_required"

    assert _auth_event_types(landscape_url) == ["approval_requested", "approval_decided", "approval_requested"]


def _seeded_engine(tmp_path: Path) -> tuple[Any, str]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(role_id="role-bob", identity_id="bob", role="approver", granted_at=now, granted_by_identity_id="alice")
        )
        conn.execute(
            insert(sessions_table).values(id=session_id, user_id="alice", auth_provider_type="local", title="r4", created_at=now, updated_at=now)
        )
    return engine, session_id


def _request_with(recorder: AuthAuditRecorder, engine: Any, session_id: str) -> Any:
    from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence

    binding = build_approval_binding(
        evidence=WebPluginPolicyEvidence(
            schema_version=1,
            policy_hash="a" * 64,
            snapshot_hash="b" * 64,
            authorized_plugin_ids=(),
            available_plugin_ids=(),
            control_modes=(),
            selected_implementations=(),
            selected_profile_aliases=(),
            plugin_code_identities=(),
            binding_generation_fingerprint="c" * 64,
            decision_codes=(),
        ),
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )
    return ApprovalTransactionAuthority(engine).run(
        session_id,
        lambda token, now: RepositoryApprovalAuthority.request(
            token,
            session_id=session_id,
            state_id="state-1",
            binding=binding,
            requested_by="alice",
            approver="bob",
            note=None,
            now=now,
            record=lambda created: recorder.record_approval_requested(None, provider="local", approval=created),
        ),
    )


@pytest.mark.integration
def test_a_failed_audit_write_rolls_the_approval_row_back(tmp_path: Path) -> None:
    """R4: the record callback runs before commit, so an audit failure leaves no approval row."""
    engine, session_id = _seeded_engine(tmp_path)
    landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
    broken = AuthAuditRecorder(landscape_url=landscape_url, landscape_passphrase=None, create_tables=True)
    broken.close()  # auth/audit.py:459-465: every later write raises
    with pytest.raises(RuntimeError, match="Auth audit recorder is closed"):
        _request_with(broken, engine, session_id)
    with engine.connect() as conn:
        assert conn.execute(select(approvals_table)).all() == []
    # Positive control: the same request with a live recorder writes both rows.
    with AuthAuditRecorder(landscape_url=landscape_url, landscape_passphrase=None, create_tables=True) as recorder:
        created = _request_with(recorder, engine, session_id)
    with engine.connect() as conn:
        assert [row.approval_id for row in conn.execute(select(approvals_table)).all()] == [created.approval_id]
    assert _auth_event_types(landscape_url) == ["approval_requested"]
```

- [ ] **Step 30: Run the integration tests.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/integration/web/workflow/test_approvals.py -n 0 > /tmp/i3-lane-integration.log 2>&1; echo exit=$?`
Expected: `exit=0`, `2 passed`. A failure at step 2 (`requested.json()["binding"] == execute_binding`) means `compile_approval_binding` and `execute_pipeline` hash different `audit_safe_config` values: diff the two dicts (log `deep_thaw(prepared.frozen_run_settings.audit_safe_config)` in both paths) before touching the test — that is the defect decision 2 exists to prevent. A run that ends `failed` with `Run admission refused: approval_binding_mismatch` means the envelope-derived binding in `_approval_gate_inputs_for_run` disagrees with the pre-flight's; same diagnosis, on `envelope.audit_safe_config`.

- [ ] **Step 31: Write the PostgreSQL concurrent-decide loser test.**

Create `tests/testcontainer/web/test_approval_decide_race_postgres.py`:

```python
"""PostgreSQL proof of the LOSING half of the concurrent-decide guard (spec §Testing :1276-1310).

Two approvers decide the same open request at the same moment on two
connections. The winner's ``decide`` locks both participant ``identities``
rows ``FOR UPDATE`` (stable id order), writes the decision row and the
conditional UPDATE, and reaches its ``record`` callback — inside its
transaction, before COMMIT. That callback is the rendezvous: it does not
return until ``pg_stat_activity`` shows the loser blocked on the shared
requester's ``identities`` row lock. Once the winner commits, the loser
re-reads the approval ``FOR UPDATE``, sees ``decision='approved'`` and raises
``ApprovalAlreadyDecided`` carrying the current state — no second
``approval_decisions`` row, no audit callback.

The deciders use raw ``engine.begin()`` transactions with a registered
mutation token rather than ``ApprovalTransactionAuthority.run``: ``run`` also
takes the session advisory lock, which would serialise the pair before
either reached the row guard this test exists to exercise. SQLite cannot
show the race (``BEGIN IMMEDIATE`` serialises writers), hence a
testcontainer test. The database is this test's own: the container is shared.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.approval_authority import ApprovalAlreadyDecided, ApprovalRecord, RepositoryApprovalAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import _register_mutation_connection, _unregister_mutation_connection
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import approval_decisions_table, approvals_table, identity_roles_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer

_BINDING_JSON = {
    "config_hash": "1" * 64,
    "canonical_version": "sha256-rfc8785-v1",
    "runtime_val_manifest_sha256": "3" * 64,
    "openrouter_catalog_sha256": "2" * 64,
    "binding_generation_fingerprint": "c" * 64,
    "policy_hash": "a" * 64,
}


@pytest.fixture
def engines(external_deployment_postgres_url: str) -> Iterator[tuple[Engine, Engine, Engine]]:
    database = f"approval_decide_race_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False)
    winner_engine = create_session_engine(url, connect_args={"application_name": "approval_winner"})
    loser_engine = create_session_engine(url, connect_args={"application_name": "approval_loser"})
    observer = create_session_engine(url)
    try:
        initialize_session_schema(winner_engine)
        now = datetime.now(UTC)
        with winner_engine.begin() as conn:
            for identity_id in ("author", "approver", "cover"):
                ensure_test_identity(conn, identity_id=identity_id)
            for identity_id in ("approver", "cover"):
                conn.execute(
                    insert(identity_roles_table).values(
                        role_id=f"{identity_id}-role", identity_id=identity_id, role="approver", granted_at=now, granted_by_identity_id="author"
                    )
                )
            conn.execute(insert(sessions_table).values(id="session", user_id="author", title="race", created_at=now, updated_at=now))
            conn.execute(
                insert(approvals_table).values(
                    approval_id="open",
                    session_id="session",
                    state_id="state",
                    binding_json=_BINDING_JSON,
                    requested_by_identity_id="author",
                    approver_identity_id="approver",
                    requested_at=now,
                )
            )
        yield winner_engine, loser_engine, observer
    finally:
        winner_engine.dispose()
        loser_engine.dispose()
        observer.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def _wait_for_identity_lock(observer: Engine, application: str) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with observer.connect() as conn:
            blocked = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = :application "
                    "AND wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND query ILIKE '%identities%')"
                ),
                {"application": application},
            ).scalar_one()
        if blocked:
            return
        sleep(0.01)
    raise AssertionError(f"{application} did not block on the identity lock")


def _decide(
    engine: Engine, *, decided_by: str, decision: Literal["approved", "rejected"], record: Any
) -> ApprovalRecord:
    with engine.begin() as conn:
        now = database_now(conn)
        token = _register_mutation_connection(conn)
        try:
            return RepositoryApprovalAuthority.decide(
                token,
                approval_id="open",
                decided_by=decided_by,
                decision=decision,
                note="not this one" if decision == "rejected" else None,
                now=now,
                record=record,
            )
        finally:
            _unregister_mutation_connection(token)


@pytest.mark.parametrize("loser_decision", ["approved", "rejected"])
def test_the_losing_concurrent_decider_gets_the_current_state_and_writes_nothing(
    engines: tuple[Engine, Engine, Engine], loser_decision: Literal["approved", "rejected"]
) -> None:
    winner_engine, loser_engine, observer = engines
    winner_ready = Event()
    release_winner = Event()
    loser_records: list[ApprovalRecord] = []

    def winner_record(_record: ApprovalRecord) -> None:
        winner_ready.set()
        if not release_winner.wait(15):
            raise AssertionError("winner release timed out")

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(_decide, winner_engine, decided_by="approver", decision="approved", record=winner_record)
        try:
            assert winner_ready.wait(10), "winner never reached its audit callback"
            loser = pool.submit(_decide, loser_engine, decided_by="cover", decision=loser_decision, record=loser_records.append)
            _wait_for_identity_lock(observer, "approval_loser")
        finally:
            release_winner.set()
        assert winner.result(timeout=15).decision == "approved"
        with pytest.raises(ApprovalAlreadyDecided, match="approval is already approved") as excinfo:
            loser.result(timeout=15)
    assert excinfo.value.current_state == "approved"
    assert loser_records == [], "the loser's audit callback must never fire"
    with observer.connect() as conn:
        decisions = conn.execute(select(approval_decisions_table)).all()
        approval = conn.execute(select(approvals_table).where(approvals_table.c.approval_id == "open")).one()
    assert [(row.decided_by_identity_id, row.decision) for row in decisions] == [("approver", "approved")]
    assert approval.decision == "approved"
```

- [ ] **Step 32: Run the PostgreSQL suites.**

This task writes sessions tables, widens the run-permit path and takes new
row locks, so the testcontainer selection runs. Docker is required; `-m
testcontainer` is required or the selection is empty and pytest exits 5.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_approval_lifecycle_postgres.py tests/testcontainer/web/test_approval_decide_race_postgres.py -m testcontainer -n 0 > /tmp/i3-lane-pg-approvals.log 2>&1; echo exit=$?`
Expected: `exit=0`; the log shows the 12 existing `test_approval_consumer_identity_lock_serializes_with_disable` cases and the 2 new `test_the_losing_concurrent_decider_gets_the_current_state_and_writes_nothing` cases passed.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_chargeable_admission_postgres.py tests/testcontainer/web/test_run_admission_custody_lock_postgres.py tests/testcontainer/web/test_session_derived_mutations_postgres.py -m testcontainer -n 0 > /tmp/i3-lane-pg-neighbours.log 2>&1; echo exit=$?`
Expected: `exit=0` (the permit path `_assess` and the composition-state head writers this task edited, on PostgreSQL).

- [ ] **Step 33: Add the changelog line.**

In `CHANGELOG.md`, directly after the `**Authentication events in signed
exports.**` bullet (:35-38, ending `history, and no historical identity
snapshot is invented from current rows.`), add:

```markdown
- **Approvals gate execution under workflow governance.** With
  `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`, a session owner requests approval of
  one composition state from an approver, who approves or rejects it (a
  rejection needs a note); the requester can withdraw an open request, and
  any new state supersedes it. A run starts only when an approved request's
  binding — config hash, canonical version, runtime validation manifest,
  OpenRouter catalog snapshot, plugin binding fingerprint and policy hash —
  matches what the run compiles, and a withdrawn approval refuses the run on
  recovery. Request and decision events are written to the Landscape
  `auth_events` table.
```

Confirm the section with the operator before the first commit: the 0.8.1
section of `CHANGELOG.md` is dated but has no release tag, so which release
this entry belongs to is a decision to check, not an inference.

- [ ] **Step 34: Lint and type-check every touched Python file.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/web/coordination/approval_authority.py src/elspeth/web/sessions/routes/workflow/__init__.py src/elspeth/web/sessions/routes/workflow/approvals.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/coordination/run_start_permit_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/service.py src/elspeth/web/execution/service.py src/elspeth/web/execution/protocol.py src/elspeth/web/execution/routes.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/sessions/test_operation_fence_wiring.py tests/unit/web/execution/test_service.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/coordination/test_approval_authority.py tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/coordination/test_state_writers_supersede_approvals.py tests/unit/web/execution/test_approval_gate_wiring.py tests/unit/web/workflow/__init__.py tests/unit/web/workflow/test_approval_routes.py tests/integration/web/workflow/__init__.py tests/integration/web/workflow/test_approvals.py tests/testcontainer/web/test_approval_decide_race_postgres.py > /tmp/i3-lane-ruff.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run the same file list through `ruff format --check` writing `/tmp/i3-lane-ruff-format.log`; expected `exit=0` (on a non-zero exit run `ruff format` over exactly those files and re-run the check).

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/web/coordination/approval_authority.py src/elspeth/web/sessions/routes/workflow/approvals.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/coordination/run_start_permit_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/service.py src/elspeth/web/execution/service.py src/elspeth/web/execution/protocol.py src/elspeth/web/execution/routes.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py > /tmp/i3-lane-mypy.log 2>&1; echo exit=$?`
Expected: `exit=0`. Never add `# type: ignore` or `# noqa` to get there (AGENTS.md Editing Rules); fix the annotation.

- [ ] **Step 35: Run the whole-tree architecture gates this task touches.**

1. Sessions database mutation authority (the whole file, not only the manifest id):

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rx > /tmp/i3-lane-gate-manifest.log 2>&1; echo exit=$?`
   Expected: `exit=0`, and the `-rx` summary shows that gate as `1 xfailed`: the gate already XFAILs on a clean HEAD (measured 2026-09-14 on a `git archive` export of 818d04577, `1 xfailed in 115.45s`), so this task cannot bring it to a pass. Read the XFAIL text instead: its counts must equal I1 Step 15's recorded baseline, and no `Unexpected/unreviewed` or `Stale reviewed` row may name a site this task touches. No step after Step 20 edits a Sessions writer function, so no row should move; an `Unexpected/unreviewed` line here means a writer function changed after its row was derived (typically a step redone after review) — re-derive that one row from the XFAIL text exactly as Step 12 did.

2. Attribute contracts (`src/elspeth/web/sessions` gained `routes/workflow/` and edits to `service.py` / `protocol.py`; none may add a `getattr`/`hasattr`):

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_sessions_composer_attribute_contracts.py -n 0 > /tmp/i3-lane-gate-attributes.log 2>&1; echo exit=$?`
   Expected: `exit=0`.

3. Masquerade sites (tests included). This block has no `getattr`/`hasattr` in production or test code (the structural tests parametrise by object), so the baseline must not move:

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/elspeth_lints/test_masquerade_gate.py -n 0 > /tmp/i3-lane-gate-masquerade.log 2>&1; echo exit=$?`
   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m elspeth_lints.rules.masquerade.seed_baseline --check > /tmp/i3-lane-gate-masquerade-check.log 2>&1; echo exit=$?`
   Expected: both `exit=0`. A red names a probe this task introduced: remove the probe (direct attribute access on the owned type), never reseed to admit it.

4. Soft-mapping census. `ApprovalBinding.from_json(value: Mapping[str, object])` is one new soft parameter in `src/elspeth/web/coordination/approval_authority.py`, so the check reds first:

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m scripts.check_contracts > /tmp/i3-lane-gate-census-1.log 2>&1; echo exit=$?`
   Expected: `exit=1`, the census drift names `src/elspeth/web/coordination/approval_authority.py` and no other file. Any other file in the drift is a site this task did not intend: read it before re-pinning.

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m scripts.check_contracts --write-census > /tmp/i3-lane-gate-census-write.log 2>&1; echo exit=$?` then `git diff -- config/cicd/soft-mapping-census.yaml > /tmp/i3-lane-gate-census.diff; echo exit=$?`
   Expected: the diff adds the `approval_authority.py` entry and moves the `totals` block by exactly that count; nothing else.

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m scripts.check_contracts > /tmp/i3-lane-gate-census-2.log 2>&1; echo exit=$?` and `pytest tests/unit/scripts/test_check_contracts.py -n 0 > /tmp/i3-lane-gate-census-test.log 2>&1; echo exit=$?`
   Expected: both `exit=0`.

5. Landscape mutation fencing (`auth/audit.py` gained two Landscape writers). Its inventory pins do not name `AuthAuditRecorder` methods on HEAD (`grep -n "record_relationship_changed" tests/unit/architecture/test_web_landscape_mutation_fencing.py` prints nothing), so no pin should move:

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_web_landscape_mutation_fencing.py -n 0 > /tmp/i3-lane-gate-fencing.log 2>&1; echo exit=$?`
   Expected: `exit=0` (its burn-down counters report as `xfail`; no `failed`).

   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python scripts/fencing_inventory.py . > /tmp/i3-lane-gate-fencing-inventory.log 2>&1; echo exit=$?`
   Expected: `exit=0` and every pin check reported as passing.

6. Trust-tier lint corpus: before/after finding sets, never a count against zero. The "before" corpus comes from a detached worktree of `HEAD` (still the base commit: nothing is committed yet), so it measures the same `elspeth_lints` over the untouched tree.

   Run: `cd "$(git rev-parse --show-toplevel)" && git worktree add --detach .claude/worktrees/i3-lints-base HEAD > /tmp/i3-lane-lints-worktree.log 2>&1; echo exit=$?`
   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --format json --root .claude/worktrees/i3-lints-base/src/elspeth --repo-root .claude/worktrees/i3-lints-base > /tmp/i3-lane-lints-before.json 2> /tmp/i3-lane-lints-before.err; echo exit=$?`
   Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --format json --root src/elspeth --repo-root . > /tmp/i3-lane-lints-after.json 2> /tmp/i3-lane-lints-after.err; echo exit=$?`
   Expected: both `exit=1` (the deliberate fail-closed corpus). Each run takes several minutes; the JSON is a list of objects with `rule_id`, `file_path`, `fingerprint`, `line`, `message`.

   Compare the finding SETS, keyed without line numbers (lines shift in every edited file) and with each tree's absolute prefix removed:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python - <<'PY'
import json
from pathlib import Path

repo = Path.cwd()
trees = {"before": repo / ".claude/worktrees/i3-lints-base", "after": repo}


def keys(label: str) -> set[tuple[str, str, str]]:
    prefix = f"{trees[label]}/"
    findings = json.loads(Path(f"/tmp/i3-lane-lints-{label}.json").read_text())
    return {(f["rule_id"], f["file_path"].replace(prefix, ""), f["message"]) for f in findings}


before, after = keys("before"), keys("after")
# Control: the corpus is known non-empty while the signing stage is open; an
# empty set means the instrument read nothing, not that the tree is clean.
assert len(before) > 0 and len(after) > 0, (len(before), len(after))
print("before", len(before), "after", len(after))
for key in sorted(after - before):
    print("ADDED", key)
for key in sorted(before - after):
    print("REMOVED", key)
PY
echo exit=$?
```

   Expected: `exit=0`, no `ADDED` line. `REMOVED` lines are fine when they name a site this task rewrote (for example a stale allowlist row whose fingerprint moved with `_assess`); an `ADDED` line is a finding this task introduced: fix the code, or, if the finding is genuinely policy-wrong, leave the clearest correct code and report it for adjudication (AGENTS.md Gotchas). Never hand-edit an allowlist signature.

   Remove the baseline worktree with the canonical script (dry run first, then act):
   `cd "$(git rev-parse --show-toplevel)" && scripts/worktree-cleanup.sh --path '.claude/worktrees/i3-lints-base' --discard-ignored > /tmp/i3-lane-lints-cleanup-dry.log 2>&1; echo exit=$?` — expected: the tree classified `REMOVABLE` (or `IGNORED`, resolved by `--discard-ignored`); then the same command with `--execute` appended, writing `/tmp/i3-lane-lints-cleanup.log`, expected `exit=0`.

- [ ] **Step 36: Run the full default suite through the canonical gate.**

Scoped runs miss cross-cutting gates (AGENTS.md Gotchas). The canonical script runs ruff, mypy, the contracts check and the default pytest selection, hashes the tree before and after, and writes one log per stage.

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,pytest > /tmp/i3-lane-full-suite-launch.log 2>&1; echo exit=$?`
Expected: `exit=0` (the child is launched); the launch log prints the run directory, its `.done` path and the wait loop to run. Run that printed wait loop (the suite takes about 20 minutes), then read `summary.txt` in the run directory: every stage `exit=0`, `frozen=YES`, and the RESULT line `0`. Do not edit the tree while it runs: a moved tree reports `frozen=NO` and the run is not evidence. On a red `pytest` stage in `e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator`, re-run the named id with `-n 0` before attributing it to this task (elspeth-0077cb7789).

- [ ] **Step 37: Commit by file pathspec.**

`tests/unit/web/execution/test_service.py`, `src/elspeth/web/sessions/service.py` and `src/elspeth/web/app.py` are files other lanes also edit. Before committing, confirm every hunk in them is this task's:

Run: `cd "$(git rev-parse --show-toplevel)" && git diff -- tests/unit/web/execution/test_service.py src/elspeth/web/sessions/service.py src/elspeth/web/app.py > /tmp/i3-lane-foreign-hunks.diff; echo exit=$?`
Expected: `exit=0`; reading the diff shows only the two permit fakes gaining `approval` (test_service.py), the permit widening and the `supersede_open_approvals` call (service.py), and the approval authority, the execution-service kwarg and the approvals router (app.py). A hunk from anyone else means stop and ask: never commit it, and never `git restore` it.

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit
```

Expected: no `[FAIL]` line (exit 0). `[WARN]` lines are read and accepted knowingly.

The twelve created files are untracked, and a commit pathspec that names an untracked path is refused, so mark exactly those twelve intent-to-add first (this records only that the paths exist; nothing else enters the index):

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/coordination/approval_authority.py src/elspeth/web/sessions/routes/workflow/__init__.py src/elspeth/web/sessions/routes/workflow/approvals.py tests/unit/web/coordination/test_approval_authority.py tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/coordination/test_state_writers_supersede_approvals.py tests/unit/web/execution/test_approval_gate_wiring.py tests/unit/web/workflow/__init__.py tests/unit/web/workflow/test_approval_routes.py tests/integration/web/workflow/__init__.py tests/integration/web/workflow/test_approvals.py tests/testcontainer/web/test_approval_decide_race_postgres.py; echo exit=$?
```

Expected: `exit=0`.

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): approvals with the R2 execute gate" -- src/elspeth/web/coordination/approval_authority.py src/elspeth/web/sessions/routes/workflow/__init__.py src/elspeth/web/sessions/routes/workflow/approvals.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/coordination/run_start_permit_authority.py src/elspeth/web/coordination/repository.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/service.py src/elspeth/web/execution/service.py src/elspeth/web/execution/protocol.py src/elspeth/web/execution/routes.py src/elspeth/web/auth/audit.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/sessions/test_operation_fence_wiring.py tests/unit/web/execution/test_service.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/coordination/test_approval_authority.py tests/unit/web/coordination/test_r2_execute_gate.py tests/unit/web/coordination/test_state_writers_supersede_approvals.py tests/unit/web/execution/test_approval_gate_wiring.py tests/unit/web/workflow/__init__.py tests/unit/web/workflow/test_approval_routes.py tests/integration/web/workflow/__init__.py tests/integration/web/workflow/test_approvals.py tests/testcontainer/web/test_approval_decide_race_postgres.py CHANGELOG.md config/cicd/soft-mapping-census.yaml
```

Run: `cd "$(git rev-parse --show-toplevel)" && git show --stat HEAD > /tmp/i3-lane-commit-stat.log 2>&1; echo exit=$?`
Expected: `exit=0`; the stat lists exactly those 31 files (12 created, 19 modified) and the summary line reads `31 files changed`. `web/app.py`, `auth/audit.py`, `tests/unit/web/auth/test_audit.py`, `tests/unit/web/auth/test_identity_admin_routes.py`, `tests/unit/architecture/test_session_db_mutation_authority.py`, `config/cicd/soft-mapping-census.yaml` and `CHANGELOG.md` are also touched by I4/I5: the second lane to land rebases, and every collision is an adjacent-line append or a census re-pin.

**Open questions (operator rulings needed before execution):**

1. **Is `superseded` audited?** The earlier working decision listed
   `superseded` in `record_approval_decided`'s decision set. This block writes no
   `auth_events` row for it: `supersede_open_approvals` runs inside the
   composition-state head writers (`append_state`, `create_or_reconcile_pending`,
   `_insert_composition_state`), which sit below the audit seam and take no
   `record` callback, and Step 5's
   `test_approval_decided_refuses_a_decision_outside_the_identity_made_set`
   plus the route test `test_sent_shows_a_new_state_superseding_the_open_request`
   (`methods() == ["record_approval_requested"]`) pin that. Ruling needed: thread a `record` callback through every
   composition-state writer so supersession writes `approval_decided`, or rule that
   `superseded` is not audited.
2. **Which `config_hash` is bound?** Decision 2 binds
   `stable_hash(deep_thaw(audit_safe_config))` from the execution envelope,
   not the Landscape `runs.config_hash`, because the latter exists only after
   the worker thread has loaded settings. Confirm that choice.
3. **Two deviations from earlier working forms.** This block does not consume
   `pg_fenced`, the PostgreSQL fixture on the shared testcontainer that an
   earlier working decision had I3 reuse along with I1's other fixture names:
   the concurrent-decide race needs two independent transactions on committed
   rows, so the PostgreSQL loser test builds its own database. And `withdraw`
   takes `now` and `record`, which the earlier signature
   `withdraw(connection_token, *, approval_id, requested_by)` omitted, because
   decision 1 requires every audited authority mutation to take a required
   `record` callback. Confirm both deviations.
