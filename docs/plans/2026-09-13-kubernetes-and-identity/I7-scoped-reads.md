### Task I7: Scoped reads — workflow inspect, approver audit view, delegated administration

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I6. Runs before: I9. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I6 (I0 → I8 → I1 → I2 → I3 → {I4 ∥ I5} → I6 → I7); I9 follows it.
I7 consumes I3's `approvals` semantics, I4's review authority and view model,
I5's router-registration line and I8's switch and fixtures. From I6 it needs
nothing by name, only the ordering. Spec: D27 (sso-design.md:83), §Frontend →
Mailbox's approver read (:1215-1227), §Future seams → Delegated administration
(:1367-1369) and → Approver's audit view (:1378-1379), the `identity_relationships`
org tree (:760-775), §Testing → Workflow governance (:1276-1310, including "a
seeded pre-existing cycle in `identity_relationships`" at :1301). Every
`path:line` below was measured on HEAD 072141b75. I0, I3, I4, I5 and I6 insert
lines above several of these sites, so on your branch find each site by the
anchor text quoted beside its line number before you edit it.

`writer_principal="workflow_inspect"` was reserved at epoch 55 (models.py:315-317
ledger entry for epoch 55, the VANguard residual schema batch, which reserves
`workflow_inspect` in `audit_access_log.writer_principal`; CHECK `ck_audit_access_log_writer_principal`
at models.py:3265; `AuditAccessWriterPrincipal` at sessions/protocol.py:276).
HEAD's current epoch is 56 (models.py:333). I0 moves it to 57 and leaves this
arm alone. I7 adds no column, no CHECK and no epoch.

Decisions this task owns:

1. **The inspect predicate is re-proved inside the write transaction, by the
   authority, and a denial writes nothing.** `RepositoryAuditAccessLogAuthority.record_workflow_inspect`
   opens `locked_session_transaction` (sessions/locking.py:280), the lock that
   `record_audit_grade_view` (audit_access_log_authority.py:50-115) and I3's
   `supersede_open` already take. That is why plain SELECTs are enough: a
   concurrent supersede or decide on the same session waits for this
   transaction. On that one connection it proves, in order: (a) the session is
   live (`archived_at IS NULL`, `FOR UPDATE`); (b) `state_id` is a composition
   state of that session; (c) the caller is an `active` identity; (d) the
   caller holds a live, deployment-wide grant (unrevoked, unexpired at database
   time, `scope IS NULL`); (e) one of two arms holds. The **approver arm**
   needs an `approver` grant plus an OPEN `approvals` row (`decision IS NULL`)
   for the pair, addressed to the caller (`approver_identity_id = caller`, the
   rows I3's `_INBOX` shows that caller) and not requested by the caller. The
   **reviewer arm** needs a `reviewer` grant plus an OPEN `review_requests`
   row for the pair (I4's predicate: `cancelled_at IS NULL` and no attestation
   on the pair with `attested_at >= requested_at`), addressed to the caller or
   unaddressed (`reviewer_identity_id IS NULL`, the rows I4's `open_for` shows),
   and not requested by the caller. Roles never cross arms. A decided,
   superseded, cancelled or attested row ends inspect access. Failing any
   check raises `WorkflowInspectDenied`, and no row is written. The row means
   "a read happened". It is not a record of attempts.
2. **The privacy gate above `audit_access_log_table` (models.py:3220-3242) is
   met by construction.** Its three items are handled as follows. (1) The
   query-arg allowlist for this writer is EMPTY. The method takes no
   `query_args` parameter and stores `{}`. (2) The IP policy is literal storage
   or NULL, the same as the audit-grade view (`request.client.host if
   request.client else None`, sessions/routes/messages.py:1174), and a test
   pins it. (3) No free-text input reaches the writer. The method takes no
   `request_path` either: the authority formats
   `WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE` from the two ids. The protocol
   signature test pins that set of parameters.
3. **The inspect body is the public projection built from the live state row.
   It mints no token.** The route uses `get_state_in_session`
   (sessions/protocol.py:4620), never `_verify_session_ownership`, because the
   caller is not the owner and the authority has already proved liveness. It
   returns `generate_public_composition_dict` (yaml_generator.py:696) validated
   through the strict `shareable_reviews.models.CompositionStateResponse`
   (:185), plus `generate_public_yaml` (:852), plus I4's attestations for the
   pair. It computes no audit readiness (see the permission-boundary note in
   `ShareableReviewService.resolve_token`, shareable_reviews/service.py:616-626)
   and never touches `app.state.shareable_review_service`, which D27 bars.
4. **The audit view reads both stores, and only the route opens the Landscape.**
   `RepositoryWorkflowScopeReader.read_audit_scope` runs on one sessions
   connection. It (a) returns no scope unless the caller holds a live
   `approver` grant, (b) walks ACTIVE outgoing `approver` edges from the caller
   breadth-first to depth `MAX_APPROVER_SCOPE_DEPTH = 8` with a visited set
   seeded with the caller, so a cycle seeded past R7 terminates and the caller
   never appears in their own scope, (c) sets `truncated` when a node at depth
   8 still has an unvisited child, and (d) reads `approvals` whose requester is
   in scope and `review_attestations` whose author is in scope, each capped at
   `MAX_AUDIT_VIEW_ROWS = 200`. The route then opens the Landscape read-only
   (the `sessions/routes/runs.py:170-191` shape: `settings.get_landscape_url()`,
   `_sqlite_database_file_missing`, `LandscapeDB.from_url(url, passphrase=settings.landscape_passphrase, create_tables=False, read_only=True)`)
   for `run_attributions` joined to `runs` and for `auth_events`, filtered by
   `identity_id IN scope` and capped at 200. The view writes no
   `audit_access_log` row: `audit_access_log.session_id` is a NOT NULL FK
   (models.py:3253-3258) and a cross-session view has no single session (see
   open question 3).
5. **Delegated administration is an authority arm, not only a route check.**
   The spec says "route-layer only" (:1369), but HEAD contradicts it:
   `RepositoryIdentityAuthority.grant_role` (identity_authority.py:2573-2646)
   re-proves deployment admin through `_verified_actor` (:1032-1041), and
   `POST /roles` (identity_admin_routes.py:588-615) sits behind
   `_require_identity_admin` (:341-351). I7 adds
   `RepositoryIdentityAuthority.grant_curator_as_approver`. Inside its own
   transaction it proves the actor is active, holds a live deployment-wide
   `approver` grant, and has an ACTIVE `approver` edge to the target (a direct
   edge: the transitive audit-view scope confers no appointment). It checks
   the edge BEFORE it reads the target row, so a caller without oversight
   learns nothing about the target. It then applies the same R8,
   service-kind, already-held and expired-occupant rules `grant_role`
   applies. `grant_role` is not edited, so its three manifest rows
   (test_session_db_mutation_authority.py:3064-3081 and :3216-3224) keep their
   fingerprints. `POST /roles` swaps `_require_identity_admin` for a dependency,
   `_require_identity_admin_or_approver`, that hides the route from anyone
   holding neither role. It stays a dependency because FastAPI resolves it
   before body validation, so a probe with a malformed body still gets 404,
   not a 422 that confirms the schema. Inside the handler, an admin takes the
   unchanged admin path. A `curator` grant with no `scope` and no console
   provenance, from a live approver, goes through the new arm. Every other
   body sees 404.
6. **Governance switch.** Inspect and the audit view refuse with HTTP 409
   `{"error_type": "workflow_governance_off", "detail": <str>}` unless
   `settings.workflow_governance == "on"` (I8), the envelope I4 and I5 use. The
   delegated curator arm refuses with 409 `{"refusal": "workflow_governance_off", "detail": <str>}`,
   using the `refusal` key that `identity_admin_routes._refused` (:371-384)
   already uses on that router. It checks the approver grant first, so a
   non-approver still sees 404. The admin arm does not depend on governance.
7. **Router shape follows I4 and I5.** Each module exports its own router
   factory (`create_workflow_inspect_router`, `create_workflow_audit_view_router`),
   which `web/app.py` registers directly after I5's `create_library_router()`
   line. This differs from I3.md's Interfaces line (`create_workflow_router()`
   plus `register_<module>_routes`); see open question 1.

**Files:**
- Create: `src/elspeth/web/coordination/workflow_scope_reader.py`
- Create: `src/elspeth/web/sessions/routes/workflow/inspect.py`, `src/elspeth/web/sessions/routes/workflow/audit_view.py` (the package `__init__.py` is I3's; I7 adds nothing to it)
- Modify: `src/elspeth/web/sessions/protocol.py:276-277` (`AuditAccessWriterPrincipal = Literal["audit_grade_view", "admin_tool", "workflow_inspect"]` / `AUDIT_GRADE_VIEW_WRITER_PRINCIPAL: Literal["audit_grade_view"] = "audit_grade_view"`; two constants follow :277), `:3213-3219` (`class AuditAccessLogWriteError(RuntimeError)`; `WorkflowInspectDenied` follows it), `:3268-3280` (`class AuditAccessLogAuthority(Protocol)`; its only member `record_audit_grade_view` ends `-> AuditAccessLogRecord` at :3280; the new member follows it)
- Modify: `src/elspeth/web/coordination/audit_access_log_authority.py:1-20` (module docstring and imports), `:115` (end of `record_audit_grade_view`, `return record`; the new statements and method follow)
- Modify: `src/elspeth/web/coordination/identity_authority.py:2646-2648` (`            return grant` closes `grant_role` at :2646; `    def revoke_role(` at :2648; the new method goes between them)
- Modify: `src/elspeth/web/auth/identity_admin_routes.py:341-351` (`async def _require_identity_admin(request: Request) -> UserIdentity:`; `_require_identity_admin_or_approver` follows it) and `:588-615` (`@router.post("/roles", response_model=RoleView, status_code=201)` / `async def grant_role(` through `return _role_view(grant)`)
- Modify: `src/elspeth/web/app.py:107` (`from elspeth.web.coordination.websocket_ticket_authority import RepositorySessionWebsocketTicketAuthority`; the reader import follows), `:164` (`from elspeth.web.shareable_reviews.routes import create_shareable_reviews_router`; the two router imports go beside I5's workflow import), `:1585-1586` (`audit_access_log_authority = RepositoryAuditAccessLogAuthority(session_engine)` / `app.state.audit_access_log_authority = audit_access_log_authority`; the reader follows :1586), `:1791` (`app.include_router(create_shareable_reviews_router())`; I5's `app.include_router(create_library_router())` follows it on your branch, and the two I7 lines follow that)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:429-433` (`_NAMED_AUTHORITY_SYMBOLS` entry `RepositoryAuditAccessLogAuthority.record_audit_grade_view`), `:868-872` (`_NAMED_AUTHORITY_SYMBOLS` entry `RepositoryIdentityAuthority.revoke_role`, the identity block's last), `:1032-1036` and `:1181-1185` (the same two entries in `_CONTAINED_CONNECTION_AUTHORITIES`), `:2053-2062` (`_REVIEWED_WRITERS` row for `record_audit_grade_view`), `:3145-3154` (`_REVIEWED_WRITERS` row `RepositoryIdentityAuthority.revoke_role` `identity_roles` `update`, `line=2681`), `:3954` (`_REVIEWED_READ_CONNECTIONS`), `:11023-11040` (`test_audit_access_log_writer_is_exactly_bound_to_its_handle_free_authority`, which asserts `len(live) == len(reviewed) == 1` today)
- Modify: `tests/unit/web/sessions/test_protocol.py:248-265` (`test_audit_access_log_authority_is_handle_free_and_pins_server_owned_fields`; the new test follows it)
- Modify: `tests/unit/web/auth/test_identity_admin_routes.py:28` (`from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority`) and `:570-579` (`test_bodies_are_strict`, the file's last test; the new tests are appended after it)
- Modify: `CHANGELOG.md:35-38` (the `**Authentication events in signed exports.**` bullet under `## 0.8.1 - 2026-09-10`; the I7 bullet goes after the I8/I3/I4/I5/I6 bullets already there)
- Test (new): `tests/unit/web/coordination/test_workflow_inspect_authority.py`, `tests/unit/web/coordination/test_workflow_scope_reader.py`, `tests/unit/web/workflow/test_workflow_inspect_routes.py`, `tests/unit/web/workflow/test_workflow_audit_view_routes.py`, `tests/testcontainer/web/test_workflow_inspect_postgres.py`

**Interfaces:**
- Consumes:
  - I8: `WebSettings.workflow_governance: Literal["off", "on"] = "off"` (read as `settings.workflow_governance == "on"`); fixtures `closed_local_settings(tmp_path) -> WebSettings` (`data_dir=tmp_path`, `auth_provider="local"`, `registration_mode="closed"`, `workflow_governance="on"`, `compartment_id="test-compartment"`) and `closed_local_app(tmp_path, closed_local_settings) -> SyncASGITestClient` in `tests/unit/web/conftest.py` (`get_current_user` overridden to `alice`, `app.state.session_service` a `DualFencedSessionServiceHarness`, `app.state.settings`, `client.app.state.phase3_engine`, and the `AuditAccessLogWriteError` → 500 handler).
  - I3: the `approvals` open/addressed semantics that `RepositoryApprovalAuthority` writes (`decision IS NULL` = open; `_INBOX` = `approver_identity_id == caller AND decision IS NULL`); the package `src/elspeth/web/sessions/routes/workflow/__init__.py`; `tests/unit/web/workflow/__init__.py`.
  - I4: `RepositoryReviewAuthority(engine: Engine)` with `attestations_for(*, session_id: str, state_id: str) -> tuple[ReviewAttestationRecord, ...]`; `ReviewAttestationRecord(attestation_id, session_id, state_id, payload_digest, reviewer_identity_id, author_identity_id, attested_at, verdict, note)`; `ReviewAttestationView` (pydantic, the same fields) in `src/elspeth/web/sessions/routes/workflow/reviews.py`; `app.state.review_authority`; the open-request predicate (`cancelled_at IS NULL` and NOT EXISTS an attestation on the pair with `attested_at >= requested_at`; addressed to the reviewer or `reviewer_identity_id IS NULL`).
  - I5: the `app.include_router(create_library_router())` line in `web/app.py` (registration anchor only).
  - I6: ordering only.
  - HEAD: `RepositoryAuditAccessLogAuthority` and its `_database_now(conn)` (audit_access_log_authority.py:23-36, :39-115); `locked_session_transaction(engine, session_id)` (sessions/locking.py:280); `AuditAccessLogRecord` (sessions/protocol.py:2942-2961); `audit_access_log_table` (models.py:3248-3270), `sessions_table` (:428), `composition_states_table` (:729), `identities_table` (:3328), `identity_roles_table` (:3406), `identity_relationships_table` (:3467), `approvals_table` (:3588), `review_requests_table` (:3701), `review_attestations_table` (:3737); `database_now(conn)` (coordination/database_clock.py:54); identity_authority.py `_IDENTITY_BY_ID` / `_IDENTITY_BY_ID_FOR_UPDATE` (:735-736), `_ROLES_OF_IDENTITY` (:745), `_ACTIVE_INCOMING_EDGES` (:757), `_is_active` (:889), `_active_grants` (:895), `_refuse_role_conflict` (:1075), `_unrevoked_grant_row` (:1044), `_new_role_grant` (:1116), `_role_values` (:1139), `_parsed_kind` (:533), `_require_nonblank` (:266), `_require_optional_text` (:271), `_require_optional_datetime` (:725), `_ensure_utc` and `_database_clock_value` (imported at :63), `AdminAuthorityRequired` (:137), `IdentityNotFound` (:142), `IdentityNotActive` (:167), `RoleAlreadyHeld` (:199), `RoleChanged` (:490), `RoleGrant` (:323), `holds_active_role` (:1285), `list_roles` (:1305); identity_admin_routes.py `_authority` (:322), `_recorder` (:327), `_provider` (:332), `_hidden` (:337), `_actor` (:354), `_refused` (:371), `_uncacheable` (:387), `_role_view` (:289), `_record_role_change` (:766); `get_current_user` (auth/middleware.py); `run_sync_in_worker` (async_workers.py:179); `state_from_record` (sessions/converters.py:29); `SessionServiceProtocol.get_state_in_session(state_id: UUID, session_id: UUID)` (sessions/protocol.py:4620; impl service.py:10346); `generate_public_composition_dict` (composer/yaml_generator.py:696), `generate_public_yaml` (:852); `shareable_reviews.models.CompositionStateResponse` (:185); `LandscapeDB.from_url(url, *, passphrase, create_tables, read_only)` (core/landscape/database.py:1877) and `.read_only_connection()`; `run_attributions_table` (core/landscape/schema.py:522), `runs_table` (:445), `auth_events_table` (:2542); `RecorderFactory(db).run_lifecycle.begin_run(config, canonical_version, *, run_id, initiated_by_user_id, auth_provider_type, openrouter_catalog_sha256, openrouter_catalog_source)` (run_lifecycle_repository.py:293) and `RecorderFactory(db).auth_audit.record_auth_event(*, event_type, outcome, provider, user_id, username, failure_category, request_id, client_host, user_agent, metadata, identity_id=None)` (auth_audit_repository.py:156); `_sqlite_database_file_missing(landscape_url)` (execution/discard_summary.py:242); `AuditIntegrityError` (contracts/errors.py:966); test helpers `engine` (tests/unit/web/conftest.py:63), `_make_session(conn, *, session_id, user_id="test_user", auth_provider_type="local")` (:73), `ensure_test_identity(conn, *, identity_id, provider="local")` (tests/fixtures/identities.py:10), `external_deployment_postgres_url` (tests/testcontainer/web/conftest.py:40), and in test_identity_admin_routes.py `_Harness` (:104), `_build` (:118), `_client` (:161), `_bearer` (:165), `_active` (:187), `harness` (:202-204).
- Produces:
  - `src/elspeth/web/sessions/protocol.py`: `WORKFLOW_INSPECT_WRITER_PRINCIPAL: Literal["workflow_inspect"] = "workflow_inspect"`; `WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE: str = "/api/workflow/inspect/{session_id}/{state_id}"`; `class WorkflowInspectDenied(RuntimeError)` (not a subclass of `AuditAccessLogWriteError`); `AuditAccessLogAuthority.record_workflow_inspect(self, *, session_id: str, state_id: str, requesting_principal: str, ip_address: str | None) -> AuditAccessLogRecord`.
  - `src/elspeth/web/coordination/audit_access_log_authority.py`: `RepositoryAuditAccessLogAuthority.record_workflow_inspect` with that signature; it raises `WorkflowInspectDenied` with one of the messages `"session is not live"`, `"state is not a composition state of the session"`, `"caller is not an active identity"`, `"no live approval or review request authorises this read"`.
  - `src/elspeth/web/coordination/workflow_scope_reader.py`: `MAX_APPROVER_SCOPE_DEPTH: Final = 8`; `MAX_AUDIT_VIEW_ROWS: Final = 200`; frozen dataclasses `ScopedApproval(approval_id: str, session_id: str, state_id: str, requested_by_identity_id: str, approver_identity_id: str, requested_at: datetime, decided_at: datetime | None, decision: str | None)`, `ScopedAttestation(attestation_id: str, session_id: str, state_id: str, payload_digest: str, reviewer_identity_id: str, author_identity_id: str, attested_at: datetime, verdict: str)`, `WorkflowAuditScope(caller_is_approver: bool, identity_ids: tuple[str, ...], truncated: bool, approvals: tuple[ScopedApproval, ...], attestations: tuple[ScopedAttestation, ...])`; `RepositoryWorkflowScopeReader(engine: Engine)` with `read_audit_scope(*, caller: str) -> WorkflowAuditScope`. Mounted as `app.state.workflow_scope_reader`.
  - `src/elspeth/web/coordination/identity_authority.py`: `RepositoryIdentityAuthority.grant_curator_as_approver(self, *, actor_identity_id: str, identity_id: str, expires_at: datetime | None, note: str | None, record: Callable[[RoleChanged], None]) -> RoleGrant`.
  - `src/elspeth/web/sessions/routes/workflow/inspect.py`: `create_workflow_inspect_router() -> APIRouter`; `GET /api/workflow/inspect/{session_id}/{state_id}` → 200 `WorkflowInspectResponse(session_id: str, state_id: str, access_log_id: str, composition_snapshot: shareable_reviews.models.CompositionStateResponse, yaml: str, attestations: list[ReviewAttestationView])` with `Cache-Control: no-store`; 404 `{"detail": "Not found"}` on any denial; 409 `{"detail": {"error_type": "workflow_governance_off", "detail": <str>}}`; 500 `audit_access_log_write_failed` on a store failure.
  - `src/elspeth/web/sessions/routes/workflow/audit_view.py`: `create_workflow_audit_view_router() -> APIRouter`; `GET /api/workflow/audit-view` → 200 `WorkflowAuditViewResponse(identity_ids: list[str], truncated: bool, runs: list[AuditViewRun], approvals: list[AuditViewApproval], attestations: list[AuditViewAttestation], auth_events: list[AuditViewAuthEvent])`, where `AuditViewRun(run_id, initiated_by_identity_id, recorded_at, status, started_at, completed_at)`, `AuditViewApproval(approval_id, session_id, state_id, requested_by_identity_id, approver_identity_id, requested_at, decided_at, decision)`, `AuditViewAttestation(attestation_id, session_id, state_id, payload_digest, reviewer_identity_id, author_identity_id, attested_at, verdict)`, `AuditViewAuthEvent(event_id, occurred_at, event_type, outcome, identity_id, metadata_json: str)`; 404 `{"detail": "Not found"}` for a caller without a live `approver` grant; 409 `workflow_governance_off`.
  - `POST /api/auth/admin/roles`: the unchanged admin path, plus the delegated arm (201 `RoleView` with `granted_by_identity_id` = the approver; 409 `{"refusal": "workflow_governance_off"}` while governance is off; the authority's refusals translated by `_refused`; 404 otherwise). I9's admin/approver UI consumes this.

- [ ] **Step 1: Capture the trust-tier lint corpus before any edit.**

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing .venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth > /tmp/i7-lane-lints-before.log 2>&1; echo exit=$?`
Expected: `exit=1`. That is the standing fail-closed corpus (AGENTS.md § Judge-signature stage). Keep the log; Step 30 diffs against it.

- [ ] **Step 2: Write the failing protocol pin.**

In `tests/unit/web/sessions/test_protocol.py`, directly after `test_audit_access_log_authority_is_handle_free_and_pins_server_owned_fields` (:248-265), append:

```python
def test_workflow_inspect_writer_is_handle_free_and_owns_its_path_principal_and_args() -> None:
    """D27's writer takes ids and the client IP and nothing else (models.py privacy gate items 1-3).

    ``request_path`` and ``query_args`` are absent on purpose: the authority
    formats the path from the two ids and stores ``{}``, so no route can pass
    a free-text payload into ``audit_access_log`` under ``workflow_inspect``.
    """
    signature = inspect.signature(AuditAccessLogAuthority.record_workflow_inspect)

    assert tuple(signature.parameters) == ("self", "session_id", "state_id", "requesting_principal", "ip_address")
    for server_owned in ("engine", "connection", "timestamp", "writer_principal", "request_path", "query_args"):
        assert server_owned not in signature.parameters
    assert signature.return_annotation == "AuditAccessLogRecord"
    assert session_protocol.WORKFLOW_INSPECT_WRITER_PRINCIPAL == "workflow_inspect"
    assert session_protocol.WORKFLOW_INSPECT_WRITER_PRINCIPAL in get_args(session_protocol.AuditAccessWriterPrincipal)
    assert session_protocol.WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE.format(session_id="s", state_id="t") == "/api/workflow/inspect/s/t"
    assert issubclass(session_protocol.WorkflowInspectDenied, RuntimeError)
    assert not issubclass(session_protocol.WorkflowInspectDenied, session_protocol.AuditAccessLogWriteError)
```

`inspect`, `get_args`, `session_protocol` and `AuditAccessLogAuthority` are already imported (test_protocol.py:5, :7, :18, :24).

- [ ] **Step 3: Run the pin to verify it fails.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_protocol.py::test_workflow_inspect_writer_is_handle_free_and_owns_its_path_principal_and_args -n 0 > /tmp/i7-lane-protocol-red.log 2>&1; echo exit=$?`
Expected: `exit=1`, with `AttributeError: type object 'AuditAccessLogAuthority' has no attribute 'record_workflow_inspect'`.

- [ ] **Step 4: Add the constants, the denial type and the protocol member.**

In `src/elspeth/web/sessions/protocol.py`, directly after `AUDIT_GRADE_VIEW_WRITER_PRINCIPAL: Literal["audit_grade_view"] = "audit_grade_view"` (:277):

```python
WORKFLOW_INSPECT_WRITER_PRINCIPAL: Literal["workflow_inspect"] = "workflow_inspect"
WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE: str = "/api/workflow/inspect/{session_id}/{state_id}"
"""The one ``request_path`` a ``workflow_inspect`` row carries; the authority formats it from the ids, never the route."""
```

Directly after the body of `class AuditAccessLogWriteError(RuntimeError):` (:3213-3219):

```python
class WorkflowInspectDenied(RuntimeError):
    """D27's per-request predicate did not hold, so nothing was written.

    Deliberately NOT an ``AuditAccessLogWriteError``: a denial is an
    authorization outcome the route hides as 404, while a write failure is a
    500 that must never be read as "you may not see this".
    """
```

Inside `class AuditAccessLogAuthority(Protocol):` (:3268), after the `record_audit_grade_view` member that ends at :3280:

```python
    def record_workflow_inspect(
        self,
        *,
        session_id: str,
        state_id: str,
        requesting_principal: str,
        ip_address: str | None,
    ) -> AuditAccessLogRecord:
        """Commit one ``workflow_inspect`` row after re-proving D27's predicate, or raise ``WorkflowInspectDenied``."""
```

- [ ] **Step 5: Run the protocol suite to verify it passes.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_protocol.py -n 0 > /tmp/i7-lane-protocol-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 6: Write the failing authority tests.**

Create `tests/unit/web/coordination/test_workflow_inspect_authority.py`:

```python
"""RepositoryAuditAccessLogAuthority.record_workflow_inspect: D27's per-request predicate (sso-design.md:1215-1227).

Each allow case is paired with a deny case that changes exactly one authority
row (the grant, the request row, its requester or addressee, the session, the
state, the caller's access state). A guard that stops reading that row turns
one side of the pair red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update

from elspeth.web.coordination import audit_access_log_authority as authority_module
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import WorkflowInspectDenied
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"
OTHER_SESSION = "44444444-4444-4444-8444-444444444444"
OTHER_STATE = "33333333-3333-4333-8333-333333333333"
T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
_EMPTY_STATE: dict[str, Any] = {"sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata_": {"name": "demo", "description": ""}}


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    """alice owns SESSION and its STATE; bob, carol and dave are active identities holding no role."""
    with engine.begin() as conn:
        _make_session(conn, session_id=SESSION, user_id="alice")
        for identity_id in ("bob", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(composition_states_table).values(
                id=STATE, session_id=SESSION, version=1, provenance="session_seed", created_at=T0, **_EMPTY_STATE
            )
        )
    return engine


def _grant(engine: Engine, identity_id: str, role: str, *, expires_at: datetime | None = None, revoked_at: datetime | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=f"role-{identity_id}-{role}",
                identity_id=identity_id,
                role=role,
                granted_by_identity_id="alice",
                granted_at=T0,
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )


def _approval(engine: Engine, *, approver: str = "bob", requested_by: str = "alice", state_id: str = STATE, decision: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id=f"ap-{approver}-{state_id}-{decision}",
                session_id=SESSION,
                state_id=state_id,
                binding_json={},
                requested_by_identity_id=requested_by,
                approver_identity_id=approver,
                requested_at=T0,
                decided_at=None if decision is None else T0 + timedelta(minutes=5),
                decision=decision,
            )
        )


def _review_request(engine: Engine, *, requested_by: str = "alice", reviewer: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(review_requests_table).values(
                request_id=f"rr-{requested_by}-{reviewer}",
                session_id=SESSION,
                state_id=STATE,
                requested_by_identity_id=requested_by,
                reviewer_identity_id=reviewer,
                requested_at=T0,
            )
        )


def _inspect(engine: Engine, caller: str, *, state_id: str = STATE, ip_address: str | None = "10.1.2.3") -> Any:
    return RepositoryAuditAccessLogAuthority(engine).record_workflow_inspect(
        session_id=SESSION, state_id=state_id, requesting_principal=caller, ip_address=ip_address
    )


def _rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(audit_access_log_table)).all())


# ── the approver arm ───────────────────────────────────────────────────────


def test_an_addressed_approver_with_an_open_request_writes_exactly_one_row(seeded: Engine) -> None:
    _grant(seeded, "bob", "approver")
    _approval(seeded)

    record = _inspect(seeded, "bob")

    rows = _rows(seeded)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == record.id
    assert row.writer_principal == "workflow_inspect"
    assert row.requesting_principal == "bob"
    assert row.session_id == SESSION
    assert row.request_path == f"/api/workflow/inspect/{SESSION}/{STATE}"
    assert row.query_args == {}
    assert row.ip_address == "10.1.2.3"


def test_the_client_ip_is_stored_literally_or_as_null(seeded: Engine) -> None:
    """Privacy gate item 2: literal storage or NULL, the audit-grade view's policy."""
    _grant(seeded, "bob", "approver")
    _approval(seeded)

    _inspect(seeded, "bob", ip_address=None)
    _inspect(seeded, "bob", ip_address="192.0.2.77")

    assert sorted((row.ip_address or "") for row in _rows(seeded)) == ["", "192.0.2.77"]


def test_the_row_timestamp_is_the_sessions_database_clock(seeded: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(authority_module, "_database_now", lambda _conn: fixed)
    _grant(seeded, "bob", "approver")
    _approval(seeded)

    assert _inspect(seeded, "bob").timestamp == fixed


def test_no_request_row_is_denied_and_writes_nothing(seeded: Engine) -> None:
    _grant(seeded, "bob", "approver")

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request authorises this read"):
        _inspect(seeded, "bob")

    assert _rows(seeded) == []


@pytest.mark.parametrize("decision", ["approved", "rejected", "superseded"])
def test_a_decided_approval_no_longer_authorises(seeded: Engine, decision: str) -> None:
    _grant(seeded, "bob", "approver")
    _approval(seeded, decision=decision)

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "bob")

    assert _rows(seeded) == []


def test_an_approval_without_the_approver_role_is_denied(seeded: Engine) -> None:
    _approval(seeded)

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "bob")


@pytest.mark.parametrize(
    ("expires_at", "revoked_at"),
    [
        pytest.param(None, T0, id="revoked"),
        pytest.param(datetime(2000, 1, 1, tzinfo=UTC), None, id="expired"),
    ],
)
def test_a_dead_approver_grant_does_not_authorise(seeded: Engine, expires_at: datetime | None, revoked_at: datetime | None) -> None:
    _grant(seeded, "bob", "approver", expires_at=expires_at, revoked_at=revoked_at)
    _approval(seeded)

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "bob")


def test_an_approval_addressed_to_another_approver_authorises_only_its_addressee(seeded: Engine) -> None:
    _grant(seeded, "bob", "approver")
    _grant(seeded, "dave", "approver")
    _approval(seeded, approver="bob")

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "dave")
    _inspect(seeded, "bob")

    assert [row.requesting_principal for row in _rows(seeded)] == ["bob"]


def test_a_reviewer_grant_does_not_open_the_approver_arm(seeded: Engine) -> None:
    _grant(seeded, "bob", "reviewer")
    _approval(seeded, approver="bob")

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "bob")


# ── the reviewer arm ───────────────────────────────────────────────────────


def test_an_open_unaddressed_review_request_authorises_any_live_reviewer(seeded: Engine) -> None:
    _grant(seeded, "carol", "reviewer")
    _review_request(seeded, reviewer=None)

    assert _inspect(seeded, "carol").writer_principal == "workflow_inspect"


def test_a_review_request_addressed_to_another_reviewer_authorises_only_its_addressee(seeded: Engine) -> None:
    _grant(seeded, "carol", "reviewer")
    _grant(seeded, "dave", "reviewer")
    _review_request(seeded, reviewer="dave")

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "carol")
    _inspect(seeded, "dave")

    assert [row.requesting_principal for row in _rows(seeded)] == ["dave"]


def test_the_requester_is_denied_and_the_same_row_admits_once_someone_else_requested_it(seeded: Engine) -> None:
    _grant(seeded, "carol", "reviewer")
    _review_request(seeded, requested_by="carol", reviewer=None)

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "carol")

    with seeded.begin() as conn:
        conn.execute(update(review_requests_table).values(requested_by_identity_id="alice"))
    _inspect(seeded, "carol")

    assert len(_rows(seeded)) == 1


def test_an_attestation_after_the_request_closes_it(seeded: Engine) -> None:
    _grant(seeded, "carol", "reviewer")
    _grant(seeded, "dave", "reviewer")
    _review_request(seeded, reviewer=None)
    with seeded.begin() as conn:
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id="at-1",
                session_id=SESSION,
                state_id=STATE,
                payload_digest="sha256:" + "0" * 64,
                reviewer_identity_id="dave",
                author_identity_id="alice",
                attested_at=T0 + timedelta(hours=1),
                verdict="signed_off",
            )
        )

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "carol")


def test_a_cancelled_review_request_does_not_authorise(seeded: Engine) -> None:
    _grant(seeded, "carol", "reviewer")
    _review_request(seeded, reviewer=None)
    with seeded.begin() as conn:
        conn.execute(update(review_requests_table).values(cancelled_at=T0 + timedelta(minutes=1)))

    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        _inspect(seeded, "carol")


# ── the subject and the caller ─────────────────────────────────────────────


def test_an_archived_session_is_denied(seeded: Engine) -> None:
    _grant(seeded, "bob", "approver")
    _approval(seeded)
    with seeded.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == SESSION).values(archived_at=T0))

    with pytest.raises(WorkflowInspectDenied, match="session is not live"):
        _inspect(seeded, "bob")

    assert _rows(seeded) == []


def test_a_state_of_another_session_is_denied_even_with_a_request_row_naming_it(seeded: Engine) -> None:
    with seeded.begin() as conn:
        _make_session(conn, session_id=OTHER_SESSION, user_id="alice")
        conn.execute(
            insert(composition_states_table).values(
                id=OTHER_STATE, session_id=OTHER_SESSION, version=1, provenance="session_seed", created_at=T0, **_EMPTY_STATE
            )
        )
    _grant(seeded, "bob", "approver")
    _approval(seeded, state_id=OTHER_STATE)

    with pytest.raises(WorkflowInspectDenied, match="state is not a composition state of the session"):
        _inspect(seeded, "bob", state_id=OTHER_STATE)


def test_a_disabled_caller_is_denied_even_with_a_live_grant_and_request(seeded: Engine) -> None:
    _grant(seeded, "bob", "approver")
    _approval(seeded)
    with seeded.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="disabled", disabled_at=T0))

    with pytest.raises(WorkflowInspectDenied, match="caller is not an active identity"):
        _inspect(seeded, "bob")
```

- [ ] **Step 7: Run the authority tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_workflow_inspect_authority.py -n 0 > /tmp/i7-lane-authority-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; every test that calls `_inspect` fails with `AttributeError: 'RepositoryAuditAccessLogAuthority' object has no attribute 'record_workflow_inspect'`.

- [ ] **Step 8: Implement `record_workflow_inspect`.**

In `src/elspeth/web/coordination/audit_access_log_authority.py`, replace lines 1-20 (the module docstring through the `elspeth.web.sessions.protocol` import) with:

```python
"""Handle-free repository authority for ``audit_access_log`` rows.

Two writers, one table, one lock. ``record_audit_grade_view`` records an
owner reading their own audit-grade transcript. ``record_workflow_inspect``
records D27's read: an approver or reviewer reading another identity's
composition state because a live request addressed to them exists.

Both re-prove their subject inside ``locked_session_transaction`` and write
nothing unless it holds. The workflow writer meets the privacy gate declared
above ``audit_access_log_table`` (sessions/models.py) by construction: it takes
no ``query_args`` (the allowlist is empty and ``{}`` is stored), no
``request_path`` (formatted here from the two ids), and stores the client IP
literally or as NULL.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, final, get_args
from uuid import uuid4

from sqlalchemy import Connection, Engine, bindparam, exists, insert, or_, select

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    AUDIT_GRADE_VIEW_WRITER_PRINCIPAL,
    WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE,
    WORKFLOW_INSPECT_WRITER_PRINCIPAL,
    AuditAccessLogRecord,
    AuditAccessLogWriteError,
    WorkflowInspectDenied,
)

# D27's predicate, as module-level statements with bound parameters so the
# Sessions writer manifest resolves every execute on the contained connection.
_LIVE_SESSION_FOR_UPDATE: Final = (
    select(sessions_table.c.id)
    .where(sessions_table.c.id == bindparam("session_id"), sessions_table.c.archived_at.is_(None))
    .with_for_update()
)
_STATE_OF_SESSION: Final = select(composition_states_table.c.id).where(
    composition_states_table.c.id == bindparam("state_id"),
    composition_states_table.c.session_id == bindparam("session_id"),
)
_ACTIVE_CALLER: Final = select(identities_table.c.identity_id).where(
    identities_table.c.identity_id == bindparam("caller"),
    identities_table.c.access_state == "active",
)
# Unrevoked, deployment-wide workflow grants; expiry is judged in Python
# against database time (SQLite stores timestamps as text).
_CALLER_WORKFLOW_GRANTS: Final = select(identity_roles_table.c.role, identity_roles_table.c.expires_at).where(
    identity_roles_table.c.identity_id == bindparam("caller"),
    identity_roles_table.c.role.in_(("approver", "reviewer")),
    identity_roles_table.c.scope.is_(None),
    identity_roles_table.c.revoked_at.is_(None),
)
# The approver arm: the open request the caller's inbox shows (I3's _INBOX).
_OPEN_APPROVAL_ADDRESSED_TO_CALLER: Final = select(approvals_table.c.approval_id).where(
    approvals_table.c.session_id == bindparam("session_id"),
    approvals_table.c.state_id == bindparam("state_id"),
    approvals_table.c.decision.is_(None),
    approvals_table.c.approver_identity_id == bindparam("caller"),
    approvals_table.c.requested_by_identity_id != bindparam("caller"),
)
# The reviewer arm: I4's derived "open" (not cancelled, not attested since).
_ATTESTED_SINCE_REQUEST: Final = exists().where(
    review_attestations_table.c.session_id == review_requests_table.c.session_id,
    review_attestations_table.c.state_id == review_requests_table.c.state_id,
    review_attestations_table.c.attested_at >= review_requests_table.c.requested_at,
)
_OPEN_REVIEW_REQUEST_FOR_CALLER: Final = select(review_requests_table.c.request_id).where(
    review_requests_table.c.session_id == bindparam("session_id"),
    review_requests_table.c.state_id == bindparam("state_id"),
    review_requests_table.c.cancelled_at.is_(None),
    ~_ATTESTED_SINCE_REQUEST,
    or_(
        review_requests_table.c.reviewer_identity_id == bindparam("caller"),
        review_requests_table.c.reviewer_identity_id.is_(None),
    ),
    review_requests_table.c.requested_by_identity_id != bindparam("caller"),
)


def _grant_is_live(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return True
    aware = expires_at.astimezone(UTC) if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return aware > now
```

Then, directly after `return record` at the end of `record_audit_grade_view` (:115), add this method to the class:

```python
    def record_workflow_inspect(
        self,
        *,
        session_id: str,
        state_id: str,
        requesting_principal: str,
        ip_address: str | None,
    ) -> AuditAccessLogRecord:
        """Commit one ``workflow_inspect`` row after re-proving D27's predicate (sso-design.md:1215-1227).

        Under the session lock, on one connection: the session is live, the
        state belongs to it, the caller is an active identity, and either the
        caller holds a live ``approver`` grant and an OPEN approval for the
        pair is addressed to them, or the caller holds a live ``reviewer``
        grant and an OPEN review request for the pair is addressed to them or
        to any reviewer. In both arms the caller is not the requester. Anything
        else raises :class:`WorkflowInspectDenied`, and nothing is written.
        """
        for field_name, value in (("session_id", session_id), ("state_id", state_id), ("requesting_principal", requesting_principal)):
            if type(value) is not str or not value:
                raise TypeError(f"{field_name} must be a non-empty exact string")
        if type(ip_address) not in {str, type(None)}:
            raise TypeError("ip_address must be an exact string or None")
        pair = {"session_id": session_id, "state_id": state_id, "caller": requesting_principal}

        with locked_session_transaction(self._engine, session_id) as conn:
            now = _database_now(conn)
            if conn.execute(_LIVE_SESSION_FOR_UPDATE, {"session_id": session_id}).one_or_none() is None:
                raise WorkflowInspectDenied("session is not live")
            if conn.execute(_STATE_OF_SESSION, {"session_id": session_id, "state_id": state_id}).one_or_none() is None:
                raise WorkflowInspectDenied("state is not a composition state of the session")
            if conn.execute(_ACTIVE_CALLER, {"caller": requesting_principal}).one_or_none() is None:
                raise WorkflowInspectDenied("caller is not an active identity")
            live_roles = {
                row.role
                for row in conn.execute(_CALLER_WORKFLOW_GRANTS, {"caller": requesting_principal}).all()
                if _grant_is_live(row.expires_at, now)
            }
            approver_arm = "approver" in live_roles and conn.execute(_OPEN_APPROVAL_ADDRESSED_TO_CALLER, pair).first() is not None
            reviewer_arm = "reviewer" in live_roles and conn.execute(_OPEN_REVIEW_REQUEST_FOR_CALLER, pair).first() is not None
            if not (approver_arm or reviewer_arm):
                raise WorkflowInspectDenied("no live approval or review request authorises this read")
            record = AuditAccessLogRecord(
                id=str(uuid4()),
                timestamp=now,
                session_id=session_id,
                requesting_principal=requesting_principal,
                request_path=WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE.format(session_id=session_id, state_id=state_id),
                query_args={},
                ip_address=ip_address,
                writer_principal=WORKFLOW_INSPECT_WRITER_PRINCIPAL,
            )
            conn.execute(
                insert(audit_access_log_table).values(
                    id=record.id,
                    timestamp=record.timestamp,
                    session_id=record.session_id,
                    requesting_principal=record.requesting_principal,
                    request_path=record.request_path,
                    query_args={},
                    ip_address=record.ip_address,
                    writer_principal=record.writer_principal,
                )
            )
        return record
```

The module-level `_database_now` (:23-36) is looked up at call time, which is what lets the clock test patch it.

- [ ] **Step 9: Run the authority tests and the existing audit-log suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_workflow_inspect_authority.py tests/unit/web/sessions/test_record_audit_grade_view.py tests/unit/web/sessions/test_audit_access_log.py tests/unit/web/sessions/test_identity_owner_schema.py tests/unit/web/sessions/test_protocol.py -n 0 > /tmp/i7-lane-authority-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. If `test_a_state_of_another_session_is_denied_even_with_a_request_row_naming_it` fails with an `IntegrityError` on the approvals insert, `approvals` carries a composite `(state_id, session_id)` FK on your branch. In that case seed the approval on `(OTHER_SESSION, OTHER_STATE)`, inspect `SESSION` with `OTHER_STATE`, and expect the same `state is not a composition state of the session` message. Change the seed, never the guard.

- [ ] **Step 10: Write the failing scope-reader tests.**

Create `tests/unit/web/coordination/test_workflow_scope_reader.py`:

```python
"""RepositoryWorkflowScopeReader: who an approver oversees, and what those people did (sso-design.md:1378).

The spec asks for "a seeded pre-existing cycle in identity_relationships"
(:1301): R7 refuses a cycle at write time (identity_authority.py
assert_relationship), so the cycle here is seeded by direct SQL, which is how
data predating R7 would look.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, update

from elspeth.web.coordination.workflow_scope_reader import (
    MAX_APPROVER_SCOPE_DEPTH,
    MAX_AUDIT_VIEW_ROWS,
    RepositoryWorkflowScopeReader,
)
from elspeth.web.sessions.models import (
    approvals_table,
    identity_relationships_table,
    identity_roles_table,
    review_attestations_table,
)
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def _identities(engine: Engine, *identity_ids: str) -> None:
    with engine.begin() as conn:
        for identity_id in ("root", *identity_ids):
            ensure_test_identity(conn, identity_id=identity_id)


def _approver(engine: Engine, identity_id: str, *, expires_at: datetime | None = None, revoked_at: datetime | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id=f"role-{identity_id}",
                identity_id=identity_id,
                role="approver",
                granted_by_identity_id="root",
                granted_at=T0,
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )


def _edge(engine: Engine, from_id: str, to_id: str, *, revoked: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id=f"edge-{from_id}-{to_id}",
                from_identity_id=from_id,
                to_identity_id=to_id,
                relationship_type="approver",
                asserted_by_identity_id="root",
                asserted_at=T0,
                revoked_at=T0 if revoked else None,
                revoked_by_identity_id="root" if revoked else None,
            )
        )


def test_the_bounds_are_the_ones_the_spec_and_the_route_promise() -> None:
    assert MAX_APPROVER_SCOPE_DEPTH == 8
    assert MAX_AUDIT_VIEW_ROWS == 200


def test_a_caller_without_a_live_approver_grant_has_no_scope_and_gains_it_with_the_grant(engine: Engine) -> None:
    _identities(engine, "boss", "a")
    _edge(engine, "boss", "a")

    before = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")
    assert before.caller_is_approver is False
    assert before.identity_ids == ()

    _approver(engine, "boss")
    after = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")
    assert after.caller_is_approver is True
    assert after.identity_ids == ("a",)


@pytest.mark.parametrize(
    ("expires_at", "revoked_at"),
    [
        pytest.param(None, T0, id="revoked"),
        pytest.param(datetime(2000, 1, 1, tzinfo=UTC), None, id="expired"),
    ],
)
def test_a_dead_approver_grant_yields_no_scope(engine: Engine, expires_at: datetime | None, revoked_at: datetime | None) -> None:
    _identities(engine, "boss", "a")
    _approver(engine, "boss", expires_at=expires_at, revoked_at=revoked_at)
    _edge(engine, "boss", "a")

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")

    assert scope.caller_is_approver is False
    assert scope.identity_ids == ()


def test_scope_is_transitive_over_active_edges_and_excludes_outsiders(engine: Engine) -> None:
    _identities(engine, "boss", "a", "b", "c", "outsider_lead", "outsider")
    _approver(engine, "boss")
    _edge(engine, "boss", "a")
    _edge(engine, "a", "b")
    _edge(engine, "boss", "c")
    _edge(engine, "outsider_lead", "outsider")

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")

    assert set(scope.identity_ids) == {"a", "b", "c"}
    assert "boss" not in scope.identity_ids
    assert scope.truncated is False


def test_a_revoked_edge_is_not_followed(engine: Engine) -> None:
    _identities(engine, "boss", "a", "b")
    _approver(engine, "boss")
    _edge(engine, "boss", "a")
    _edge(engine, "a", "b", revoked=True)

    assert RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss").identity_ids == ("a",)


def test_depth_stops_at_eight_and_the_ninth_hop_is_reported_as_truncated(engine: Engine) -> None:
    chain = [f"n{index}" for index in range(1, 10)]
    _identities(engine, "boss", *chain)
    _approver(engine, "boss")
    previous = "boss"
    for node in chain:
        _edge(engine, previous, node)
        previous = node

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")
    assert scope.identity_ids == tuple(chain[:8])
    assert scope.truncated is True

    with engine.begin() as conn:
        conn.execute(
            update(identity_relationships_table)
            .where(identity_relationships_table.c.to_identity_id == "n9")
            .values(revoked_at=T0, revoked_by_identity_id="root")
        )
    uncut = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")
    assert uncut.identity_ids == tuple(chain[:8])
    assert uncut.truncated is False


def test_a_seeded_cycle_terminates_and_never_returns_the_caller(engine: Engine) -> None:
    _identities(engine, "boss", "a", "b")
    _approver(engine, "boss")
    _edge(engine, "boss", "a")
    _edge(engine, "a", "b")
    _edge(engine, "b", "boss")

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")

    assert scope.identity_ids == ("a", "b")
    assert scope.truncated is False


def test_approvals_and_attestations_are_limited_to_the_scoped_identities(engine: Engine) -> None:
    _identities(engine, "boss", "a", "outsider", "rev")
    _approver(engine, "boss")
    _edge(engine, "boss", "a")
    with engine.begin() as conn:
        _make_session(conn, session_id="session-a", user_id="a")
        _make_session(conn, session_id="session-outsider", user_id="outsider")
        for approval_id, session_id, requester in (("ap-a", "session-a", "a"), ("ap-outsider", "session-outsider", "outsider")):
            conn.execute(
                insert(approvals_table).values(
                    approval_id=approval_id,
                    session_id=session_id,
                    state_id=f"state-{requester}",
                    binding_json={},
                    requested_by_identity_id=requester,
                    approver_identity_id="boss",
                    requested_at=T0,
                )
            )
        for attestation_id, session_id, author in (("at-a", "session-a", "a"), ("at-outsider", "session-outsider", "outsider")):
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=attestation_id,
                    session_id=session_id,
                    state_id=f"state-{author}",
                    payload_digest="sha256:" + "0" * 64,
                    reviewer_identity_id="rev",
                    author_identity_id=author,
                    attested_at=T0,
                    verdict="signed_off",
                )
            )

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")

    assert [row.approval_id for row in scope.approvals] == ["ap-a"]
    assert [row.attestation_id for row in scope.attestations] == ["at-a"]


def test_rows_are_capped_at_the_view_bound(engine: Engine) -> None:
    _identities(engine, "boss", "a")
    _approver(engine, "boss")
    _edge(engine, "boss", "a")
    with engine.begin() as conn:
        _make_session(conn, session_id="session-a", user_id="a")
        for index in range(MAX_AUDIT_VIEW_ROWS + 1):
            conn.execute(
                insert(approvals_table).values(
                    approval_id=f"ap-{index:04d}",
                    session_id="session-a",
                    state_id=f"state-{index:04d}",
                    binding_json={},
                    requested_by_identity_id="a",
                    approver_identity_id="boss",
                    requested_at=T0 + timedelta(seconds=index),
                )
            )

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="boss")

    assert len(scope.approvals) == MAX_AUDIT_VIEW_ROWS
    assert scope.approvals[0].approval_id == f"ap-{MAX_AUDIT_VIEW_ROWS:04d}"
```

- [ ] **Step 11: Run the scope-reader tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_workflow_scope_reader.py -n 0 > /tmp/i7-lane-scope-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (`Interrupted: 1 error during collection`), with `ModuleNotFoundError: No module named 'elspeth.web.coordination.workflow_scope_reader'`.

- [ ] **Step 12: Write the scope reader.**

Create `src/elspeth/web/coordination/workflow_scope_reader.py`:

```python
"""Read-only scope for the approver's audit view (sso-design.md:1378-1379).

``identity_relationships`` x (``approvals``, ``review_attestations``), scoped
to the caller's ACTIVE ``approver`` edges. The org tree carries exactly this
job (sessions/models.py above ``identity_relationships_table``).

The walk is breadth-first over active outgoing edges, bounded at
``MAX_APPROVER_SCOPE_DEPTH`` hops, with a visited set seeded with the caller.
R7 refuses a cycle on insert, but data predating R7 may hold one, and the
visited set is what makes the walk terminate on it. ``truncated`` says the
bound, not the tree, ended the walk.

This module opens only the Sessions store. The Landscape half of the view
(``run_attributions``, ``auth_events``) is read by the route, because
Sessions-side modules never open the Landscape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, final

from sqlalchemy import bindparam, select
from sqlalchemy.engine import Engine

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import (
    approvals_table,
    identity_relationships_table,
    identity_roles_table,
    review_attestations_table,
)

MAX_APPROVER_SCOPE_DEPTH: Final = 8
MAX_AUDIT_VIEW_ROWS: Final = 200

_CALLER_APPROVER_GRANTS: Final = select(identity_roles_table.c.expires_at).where(
    identity_roles_table.c.identity_id == bindparam("caller"),
    identity_roles_table.c.role == "approver",
    identity_roles_table.c.scope.is_(None),
    identity_roles_table.c.revoked_at.is_(None),
)
_ACTIVE_OUTGOING_EDGES: Final = (
    select(identity_relationships_table.c.to_identity_id)
    .where(
        identity_relationships_table.c.from_identity_id == bindparam("from_identity_id"),
        identity_relationships_table.c.relationship_type == "approver",
        identity_relationships_table.c.revoked_at.is_(None),
    )
    .order_by(identity_relationships_table.c.to_identity_id)
)
_APPROVALS_REQUESTED_BY_SCOPE: Final = (
    select(approvals_table)
    .where(approvals_table.c.requested_by_identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(approvals_table.c.requested_at.desc(), approvals_table.c.approval_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)
_ATTESTATIONS_AUTHORED_BY_SCOPE: Final = (
    select(review_attestations_table)
    .where(review_attestations_table.c.author_identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(review_attestations_table.c.attested_at.desc(), review_attestations_table.c.attestation_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


@final
@dataclass(frozen=True, slots=True)
class ScopedApproval:
    approval_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: str | None


@final
@dataclass(frozen=True, slots=True)
class ScopedAttestation:
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: str


@final
@dataclass(frozen=True, slots=True)
class WorkflowAuditScope:
    caller_is_approver: bool
    identity_ids: tuple[str, ...]
    truncated: bool
    approvals: tuple[ScopedApproval, ...]
    attestations: tuple[ScopedAttestation, ...]


_NO_SCOPE: Final = WorkflowAuditScope(caller_is_approver=False, identity_ids=(), truncated=False, approvals=(), attestations=())


@final
class RepositoryWorkflowScopeReader:
    """One Sessions read connection per call; writes nothing."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_audit_scope(self, *, caller: str) -> WorkflowAuditScope:
        if type(caller) is not str or not caller:
            raise TypeError("caller must be a non-empty exact string")
        with self._engine.connect() as conn:
            now = database_now(conn)
            grants = conn.execute(_CALLER_APPROVER_GRANTS, {"caller": caller}).all()
            if not any(row.expires_at is None or _utc(row.expires_at) > now for row in grants):
                return _NO_SCOPE

            visited: set[str] = {caller}
            ordered: list[str] = []
            frontier: list[str] = [caller]
            for _hop in range(MAX_APPROVER_SCOPE_DEPTH):
                next_frontier: list[str] = []
                for parent in frontier:
                    for child in conn.execute(_ACTIVE_OUTGOING_EDGES, {"from_identity_id": parent}).scalars().all():
                        if child not in visited:
                            visited.add(child)
                            ordered.append(child)
                            next_frontier.append(child)
                frontier = next_frontier
                if not frontier:
                    break
            truncated = any(
                child not in visited
                for parent in frontier
                for child in conn.execute(_ACTIVE_OUTGOING_EDGES, {"from_identity_id": parent}).scalars().all()
            )
            if not ordered:
                return WorkflowAuditScope(caller_is_approver=True, identity_ids=(), truncated=False, approvals=(), attestations=())

            approval_rows = conn.execute(_APPROVALS_REQUESTED_BY_SCOPE, {"identity_ids": ordered}).all()
            attestation_rows = conn.execute(_ATTESTATIONS_AUTHORED_BY_SCOPE, {"identity_ids": ordered}).all()

        return WorkflowAuditScope(
            caller_is_approver=True,
            identity_ids=tuple(ordered),
            truncated=truncated,
            approvals=tuple(
                ScopedApproval(
                    approval_id=row.approval_id,
                    session_id=row.session_id,
                    state_id=row.state_id,
                    requested_by_identity_id=row.requested_by_identity_id,
                    approver_identity_id=row.approver_identity_id,
                    requested_at=_utc(row.requested_at),
                    decided_at=_utc_or_none(row.decided_at),
                    decision=row.decision,
                )
                for row in approval_rows
            ),
            attestations=tuple(
                ScopedAttestation(
                    attestation_id=row.attestation_id,
                    session_id=row.session_id,
                    state_id=row.state_id,
                    payload_digest=row.payload_digest,
                    reviewer_identity_id=row.reviewer_identity_id,
                    author_identity_id=row.author_identity_id,
                    attested_at=_utc(row.attested_at),
                    verdict=row.verdict,
                )
                for row in attestation_rows
            ),
        )
```

- [ ] **Step 13: Run the scope-reader tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_workflow_scope_reader.py -n 0 > /tmp/i7-lane-scope-green.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 14: Write the failing delegated-administration route tests.**

In `tests/unit/web/auth/test_identity_admin_routes.py`, extend the identity-authority import at :28 to
`from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority, RoleGrant`,
then append after `test_bodies_are_strict` (:570-579, the file's last test):

```python
# ── delegated administration (I7, spec :1367) ───────────────────────────


def _govern(harness: _Harness) -> None:
    """Closed registration, governance on: the shape every workflow-governance test runs against (R11)."""
    harness.app.state.settings = harness.app.state.settings.model_copy(update={"registration_mode": "closed", "workflow_governance": "on"})


async def _approver_role(client: AsyncClient, root: dict[str, str], identity_id: str) -> str:
    granted = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": identity_id, "role": "approver"})
    assert granted.status_code == 201, granted.text
    return str(granted.json()["role_id"])


async def _edge(client: AsyncClient, root: dict[str, str], from_id: str, to_id: str) -> str:
    asserted = await client.post(
        "/api/auth/admin/relationships",
        headers=root,
        json={"from_identity_id": from_id, "to_identity_id": to_id, "relationship_type": "approver"},
    )
    assert asserted.status_code == 201, asserted.text
    return str(asserted.json()["relationship_id"])


def _curator_grants(harness: _Harness, identity_id: str) -> list[RoleGrant]:
    grants = harness.authority.list_roles(identity_id=identity_id, include_revoked=True, limit=50, offset=0)
    return [grant for grant in grants if grant.role == "curator"]


async def test_an_approver_appoints_a_curator_over_an_identity_they_oversee(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        await _edge(client, root, bob_id, carol_id)
        _govern(harness)
        granted = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator", "note": "library gate"})

    assert granted.status_code == 201, granted.text
    body = granted.json()
    assert (body["identity_id"], body["role"], body["granted_by_identity_id"], body["note"]) == (carol_id, "curator", bob_id, "library gate")
    changes = [call for call in harness.audit.calls if call.method == "record_role_changed" and call.kwargs["role"] == "curator"]
    assert len(changes) == 1
    assert changes[0].kwargs["actor_identity_id"] == bob_id
    assert changes[0].kwargs["change"] == "granted"
    assert changes[0].request_bound is True


async def test_the_delegated_grant_derives_from_a_live_edge(harness: _Harness) -> None:
    """Fire: no edge is hidden. Derivation: an edge admits; the same edge revoked refuses again."""
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    dave_id = _active(harness, "dave")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        _govern(harness)

        no_edge = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator"})
        assert no_edge.status_code == 404, no_edge.text
        assert no_edge.json() == {"detail": "Not found"}

        await _edge(client, root, bob_id, carol_id)
        with_edge = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator"})
        assert with_edge.status_code == 201, with_edge.text

        dave_edge = await _edge(client, root, bob_id, dave_id)
        revoked = await client.post(f"/api/auth/admin/relationships/{dave_edge}/revoke", headers=root, json={})
        assert revoked.status_code == 200, revoked.text
        after_revoke = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": dave_id, "role": "curator"})
        assert after_revoke.status_code == 404, after_revoke.text

    assert len(_curator_grants(harness, carol_id)) == 1
    assert _curator_grants(harness, dave_id) == []


async def test_the_delegated_arm_grants_curator_and_nothing_else(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        await _edge(client, root, bob_id, carol_id)
        _govern(harness)
        for body in (
            {"identity_id": carol_id, "role": "reviewer"},
            {"identity_id": carol_id, "role": "approver"},
            {"identity_id": carol_id, "role": "user"},
            {"identity_id": carol_id, "role": "admin"},
            {"identity_id": carol_id, "role": "curator", "scope": "team-a"},
            {"identity_id": carol_id, "role": "curator", "on_behalf_of": "console"},
        ):
            response = await client.post("/api/auth/admin/roles", headers=bob, json=body)
            assert response.status_code == 404, (body, response.text)

    assert [grant.role for grant in harness.authority.list_roles(identity_id=carol_id, include_revoked=True, limit=50, offset=0)] == []


async def test_a_revoked_approver_role_closes_the_delegated_arm(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        role_id = await _approver_role(client, root, bob_id)
        await _edge(client, root, bob_id, carol_id)
        revoked = await client.post(f"/api/auth/admin/roles/{role_id}/revoke", headers=root, json={})
        assert revoked.status_code == 200, revoked.text
        _govern(harness)
        response = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator"})

    assert response.status_code == 404, response.text
    assert _curator_grants(harness, carol_id) == []


async def test_the_delegated_arm_is_refused_while_governance_is_off_and_admitted_once_on(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        await _edge(client, root, bob_id, carol_id)

        off = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator"})
        assert off.status_code == 409, off.text
        assert off.json()["detail"]["refusal"] == "workflow_governance_off"

        _govern(harness)
        on = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": carol_id, "role": "curator"})
        assert on.status_code == 201, on.text


async def test_r8_still_refuses_a_curator_grant_onto_an_admin(harness: _Harness) -> None:
    """The authority's R8 applies to the delegated arm too: an approver edge onto an admin does not bypass it."""
    bob_id = _active(harness, "bob")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        await _edge(client, root, bob_id, harness.root_identity_id)
        _govern(harness)
        response = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": harness.root_identity_id, "role": "curator"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["refusal"] == "role_forbidden_for_identity"


async def test_a_caller_holding_neither_role_sees_404_even_with_a_malformed_body(harness: _Harness) -> None:
    """Regression guard for the hidden surface: the role check is a dependency, so it runs before body validation."""
    _active(harness, "bob")
    async with _client(harness.app) as client:
        bob = await _bearer(client, "bob")
        response = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": "x", "role": "not-a-role"})

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}


async def test_an_approver_cannot_appoint_themselves(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        bob = await _bearer(client, "bob")
        await _approver_role(client, root, bob_id)
        _govern(harness)
        response = await client.post("/api/auth/admin/roles", headers=bob, json={"identity_id": bob_id, "role": "curator"})

    assert response.status_code == 404, response.text
    assert _curator_grants(harness, bob_id) == []
```

`dave` needs no password: `_active` (:187) calls `ensure_identity` directly, and dave never logs in.

- [ ] **Step 15: Run the delegated tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_identity_admin_routes.py -n 0 -k "curator or delegated or appoint" > /tmp/i7-lane-delegated-red.log 2>&1; echo exit=$?`
Expected: `exit=1`. `test_an_approver_appoints_a_curator_over_an_identity_they_oversee` fails `assert 404 == 201` (`_require_identity_admin` hides the route from bob), and the governance-off test fails `assert 404 == 409`. The tests that expect 404 already pass for the wrong reason, which is why the 201 derivations are in the same tests. `test_a_caller_holding_neither_role_sees_404_even_with_a_malformed_body` passes on HEAD and must still pass after Step 17: it goes red (422) if the role check moves into the handler body.

- [ ] **Step 16: Add `grant_curator_as_approver` to the identity authority.**

In `src/elspeth/web/coordination/identity_authority.py`, between `            return grant` (:2646, the end of `grant_role`) and `    def revoke_role(` (:2648), add:

```python
    def grant_curator_as_approver(
        self,
        *,
        actor_identity_id: str,
        identity_id: str,
        expires_at: datetime | None,
        note: str | None,
        record: Callable[[RoleChanged], None],
    ) -> RoleGrant:
        """Delegated administration (sso-design.md:1367): an approver appoints a ``curator``.

        The route admits the call, and this transaction arbitrates it. On the
        rows as they stand now, it proves the actor is an ``active`` identity
        holding a live deployment-wide ``approver`` grant, and that an ACTIVE
        ``approver`` edge runs from the actor to the target. It must be a
        direct edge: the audit view's transitive scope confers no appointment.
        Anything short of that is ``AdminAuthorityRequired``, which the route
        hides as 404. It is raised BEFORE the target row is read, so a caller
        without oversight learns nothing about the target.
        ``effective_from`` / ``effective_until`` are annotations only
        (sessions/models.py) and are not read. After that, R8, the service-kind
        rule, already-held and the expired-occupant bookkeeping apply exactly
        as ``grant_role`` applies them. There is no console provenance: this arm
        is a human approver acting for themselves.
        """
        _require_nonblank(actor_identity_id, "actor_identity_id")
        _require_nonblank(identity_id, "identity_id")
        _require_optional_text(note, "note")
        _require_optional_datetime(expires_at, "expires_at")
        with self._engine.begin() as conn:
            now = _database_clock_value(conn.exec_driver_sql(self._clock_sql).scalar_one())
            actor_row = conn.execute(_IDENTITY_BY_ID, {"identity_id": actor_identity_id}).one_or_none()
            if actor_row is None or actor_row.access_state != "active":
                raise AdminAuthorityRequired()
            actor_grants = _active_grants(conn.execute(_ROLES_OF_IDENTITY, {"identity_id": actor_identity_id}).all(), now)
            if not any(grant.role == "approver" and grant.scope is None for grant in actor_grants):
                raise AdminAuthorityRequired()
            overseers = conn.execute(_ACTIVE_INCOMING_EDGES, {"to_identity_id": identity_id, "relationship_type": "approver"}).scalars().all()
            if actor_identity_id not in overseers:
                raise AdminAuthorityRequired()
            row = conn.execute(_IDENTITY_BY_ID_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None:
                raise IdentityNotFound()
            if row.access_state != "active":
                raise IdentityNotActive()
            if expires_at is not None and _ensure_utc(expires_at) <= now:
                raise ValueError("expires_at must be in the future")
            kind = _parsed_kind(row.kind, identity_id=identity_id)
            role_rows = conn.execute(_ROLES_OF_IDENTITY, {"identity_id": identity_id}).all()
            _refuse_role_conflict(kind=kind, role="curator", held=_active_grants(role_rows, now))
            occupant = _unrevoked_grant_row(role_rows, role="curator", scope=None)
            if occupant is not None:
                if _is_active(occupant.expires_at, occupant.revoked_at, now):
                    raise RoleAlreadyHeld()
                conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == occupant.role_id).values(revoked_at=now))
            grant = _new_role_grant(
                identity_id=identity_id,
                role="curator",
                scope=None,
                expires_at=None if expires_at is None else _ensure_utc(expires_at),
                note=note,
                granted_by_identity_id=actor_identity_id,
                now=now,
            )
            conn.execute(insert(identity_roles_table).values(**_role_values(grant)))
            record(
                RoleChanged(
                    grant=grant,
                    actor_identity_id=actor_identity_id,
                    note=note,
                    at=now,
                    on_behalf_of=None,
                    console_request_id=None,
                )
            )
            return grant
```

- [ ] **Step 17: Route `POST /roles` through both arms.**

In `src/elspeth/web/auth/identity_admin_routes.py`, directly after `_require_identity_admin` (:341-351), add:

```python
async def _require_identity_admin_or_approver(request: Request) -> UserIdentity:
    """Hide ``POST /roles`` from everyone but a live ``admin`` or a live ``approver`` (the I7 delegated arm).

    A dependency, not a handler check: FastAPI resolves dependencies before it
    validates the body, so a caller holding neither role who sends a malformed
    body still sees 404 instead of a 422 that confirms the route and its
    schema. The handler then decides which arm applies.
    """
    user = await get_current_user(request)
    authority = _authority(request)
    if await run_sync_in_worker(authority.holds_active_role, identity_id=user.user_id, role="admin"):
        return user
    if await run_sync_in_worker(authority.holds_active_role, identity_id=user.user_id, role="approver"):
        return user
    raise _hidden()
```

Then replace :588-615 (the `@router.post("/roles", response_model=RoleView, status_code=201)` decorator through the handler's `return _role_view(grant)`) with:

```python
    @router.post("/roles", response_model=RoleView, status_code=201)
    async def grant_role(
        request: Request,
        response: Response,
        body: GrantRoleRequest,
        user: Annotated[UserIdentity, Depends(_require_identity_admin_or_approver)],
    ) -> RoleView:
        """Admins grant any role. An approver may appoint a ``curator`` over someone they directly oversee (spec :1367).

        The admin arm is unchanged and does not depend on workflow governance.
        The delegated arm admits only ``role="curator"`` with no ``scope`` and
        no console provenance, from a live ``approver``. Every other caller sees
        404, which is the dependency's old answer. A live approver on a
        deployment with governance off gets a named 409, not a silent 404.
        """
        authority = _authority(request)
        provider = _provider(request)
        recorder = _recorder(request)

        def record(event: RoleChanged) -> None:
            _record_role_change(recorder, request, provider, event, change="granted")

        if await run_sync_in_worker(authority.holds_active_role, identity_id=user.user_id, role="admin"):
            try:
                grant = await run_sync_in_worker(
                    authority.grant_role,
                    actor=_actor(user, body),
                    identity_id=body.identity_id,
                    role=body.role,
                    scope=body.scope,
                    expires_at=body.expires_at,
                    note=body.note,
                    record=record,
                )
            except IdentityAuthorityRefusal as exc:
                raise _refused(exc) from exc
            _uncacheable(response)
            return _role_view(grant)

        if body.role != "curator" or body.scope is not None or body.on_behalf_of is not None or body.console_request_id is not None:
            raise _hidden()
        settings: WebSettings = request.app.state.settings
        if settings.workflow_governance != "on":
            raise HTTPException(
                status_code=409,
                detail={
                    "refusal": "workflow_governance_off",
                    "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)",
                },
            )
        try:
            grant = await run_sync_in_worker(
                authority.grant_curator_as_approver,
                actor_identity_id=user.user_id,
                identity_id=body.identity_id,
                expires_at=body.expires_at,
                note=body.note,
                record=record,
            )
        except IdentityAuthorityRefusal as exc:
            raise _refused(exc) from exc
        _uncacheable(response)
        return _role_view(grant)
```

The approver re-check is gone from the handler because the dependency already proved one of the two roles, and the authority re-proves the approver grant inside its transaction. `Annotated`, `Depends`, `HTTPException`, `get_current_user`, `UserIdentity`, `WebSettings` and `IdentityAuthorityRefusal` are already imported (identity_admin_routes.py:47-82).

- [ ] **Step 18: Run the whole identity-admin route file to verify it passes.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i7-lane-delegated-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. `test_unauthenticated_and_non_admin_callers_see_nothing` (:210) must still pass: bob's `{"role": "user"}` body takes the hidden branch.

- [ ] **Step 19: Write the failing inspect-route tests.**

Create `tests/unit/web/workflow/test_workflow_inspect_routes.py`:

```python
"""GET /api/workflow/inspect/{session_id}/{state_id} on I8's closed, governance-on local app.

The authority under the route is REAL; test_workflow_inspect_authority.py
pins the predicate. What these tests pin is the wire: 409 while governance is
off, a hidden 404 on any denial, the public projection on success, exactly one
access row per read, and no bearer capability.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.routes.workflow.inspect import create_workflow_inspect_router
from tests.fixtures.identities import ensure_test_identity

SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"
URL = f"/api/workflow/inspect/{SESSION}/{STATE}"
T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    """closed_local_app with alice's session and state, bob as approver, carol and alice as reviewers, and the inspect router."""
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        for identity_id in ("bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id, role in (
            ("role-bob", "bob", "approver"),
            ("role-carol", "carol", "reviewer"),
            ("role-alice", "alice", "reviewer"),
        ):
            conn.execute(
                insert(identity_roles_table).values(role_id=role_id, identity_id=identity_id, role=role, granted_by_identity_id="alice", granted_at=T0)
            )
        conn.execute(insert(sessions_table).values(id=SESSION, user_id="alice", auth_provider_type="local", title="inspect me", created_at=T0, updated_at=T0))
        conn.execute(
            insert(composition_states_table).values(
                id=STATE,
                session_id=SESSION,
                version=1,
                provenance="session_seed",
                created_at=T0,
                sources={},
                nodes=[],
                edges=[],
                outputs=[],
                metadata_={"name": "demo", "description": ""},
            )
        )
    closed_local_app.app.state.audit_access_log_authority = RepositoryAuditAccessLogAuthority(engine)
    closed_local_app.app.state.review_authority = RepositoryReviewAuthority(engine)
    closed_local_app.app.include_router(create_workflow_inspect_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def _governance(client: Any, value: str) -> None:
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": value})


def _open_approval(client: Any) -> None:
    with client.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(approvals_table).values(
                approval_id="ap-1",
                session_id=SESSION,
                state_id=STATE,
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="bob",
                requested_at=T0,
            )
        )


def _access_rows(client: Any) -> list[Any]:
    with client.app.state.phase3_engine.connect() as conn:
        return list(conn.execute(select(audit_access_log_table)).all())


def test_governance_off_refuses_with_409_and_writes_nothing(app: Any) -> None:
    _open_approval(app)
    _as(app, "bob")
    _governance(app, "off")

    response = app.get(URL)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_type"] == "workflow_governance_off"
    assert _access_rows(app) == []


def test_an_addressed_approver_reads_the_public_projection_and_one_access_row_is_written(app: Any) -> None:
    _open_approval(app)
    _as(app, "bob")

    response = app.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"session_id", "state_id", "access_log_id", "composition_snapshot", "yaml", "attestations"}
    assert (body["session_id"], body["state_id"]) == (SESSION, STATE)
    assert body["composition_snapshot"]["metadata"] == {"name": "demo", "description": ""}
    assert body["attestations"] == []
    assert isinstance(body["yaml"], str)
    assert response.headers["cache-control"] == "no-store"
    rows = _access_rows(app)
    assert [(row.id, row.writer_principal, row.requesting_principal) for row in rows] == [(body["access_log_id"], "workflow_inspect", "bob")]


def test_without_a_live_request_the_route_is_hidden_and_writes_nothing(app: Any) -> None:
    _as(app, "bob")

    response = app.get(URL)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}
    assert _access_rows(app) == []


def test_the_requester_is_hidden_and_another_reviewer_on_the_same_request_is_not(app: Any) -> None:
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(review_requests_table).values(
                request_id="rr-1", session_id=SESSION, state_id=STATE, requested_by_identity_id="alice", reviewer_identity_id=None, requested_at=T0
            )
        )

    _as(app, "alice")
    assert app.get(URL).status_code == 404
    _as(app, "carol")
    assert app.get(URL).status_code == 200

    assert [row.requesting_principal for row in _access_rows(app)] == ["carol"]


def test_attestations_on_the_pair_are_returned(app: Any) -> None:
    _open_approval(app)
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id="at-1",
                session_id=SESSION,
                state_id=STATE,
                payload_digest="sha256:" + "0" * 64,
                reviewer_identity_id="carol",
                author_identity_id="alice",
                attested_at=T0,
                verdict="signed_off",
                note="looks right",
            )
        )
    _as(app, "bob")

    body = app.get(URL).json()

    assert [(item["attestation_id"], item["reviewer_identity_id"], item["verdict"], item["note"]) for item in body["attestations"]] == [
        ("at-1", "carol", "signed_off", "looks right")
    ]


def test_the_read_does_not_use_the_shareable_review_capability(app: Any) -> None:
    """D27: never the 30-day bearer transport. The fixture does not mount it, so any use of it would 500."""
    _open_approval(app)
    _as(app, "bob")
    with pytest.raises(AttributeError, match="shareable_review_service"):
        _ = app.app.state.shareable_review_service

    response = app.get(URL)

    assert response.status_code == 200, response.text
    assert "token" not in response.json()
    assert "expires_at" not in response.json()
```

- [ ] **Step 20: Run the inspect-route tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_workflow_inspect_routes.py -n 0 > /tmp/i7-lane-inspect-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (`Interrupted: 1 error during collection`), with `ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow.inspect'`.

- [ ] **Step 21: Write the inspect route.**

Create `src/elspeth/web/sessions/routes/workflow/inspect.py`:

```python
"""GET /api/workflow/inspect/{session_id}/{state_id}: an approver's or reviewer's read of another identity's state (D27).

Authorized per request, minting no token (sso-design.md:1215-1227). The
authority re-proves the predicate and writes the ``workflow_inspect`` access
row inside the session lock BEFORE any state is read. A denial is a hidden
404, identical for "no such session", "not addressed to you" and "already
decided", so the route does not confirm what exists. The body is the public
projection that the library and the shared view also serve. It is never the
shareable-review bearer capability, and audit readiness is not recomputed here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.yaml_generator import generate_public_composition_dict, generate_public_yaml
from elspeth.web.config import WebSettings
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import (
    AuditAccessLogAuthority,
    AuditAccessLogWriteError,
    SessionServiceProtocol,
    WorkflowInspectDenied,
)
from elspeth.web.sessions.routes.workflow.reviews import ReviewAttestationView
from elspeth.web.shareable_reviews.models import CompositionStateResponse as PublicCompositionStateResponse


class WorkflowInspectResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    state_id: str
    access_log_id: str
    composition_snapshot: PublicCompositionStateResponse
    yaml: str
    attestations: list[ReviewAttestationView]


def _require_governance(request: Request) -> None:
    settings: WebSettings = request.app.state.settings
    if settings.workflow_governance != "on":
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": "workflow_governance_off",
                "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)",
            },
        )


def create_workflow_inspect_router() -> APIRouter:
    router = APIRouter(tags=["workflow-inspect"])

    @router.get("/api/workflow/inspect/{session_id}/{state_id}", response_model=WorkflowInspectResponse)
    async def inspect_workflow_state(
        session_id: UUID,
        state_id: UUID,
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> WorkflowInspectResponse:
        _require_governance(request)
        authority: AuditAccessLogAuthority = request.app.state.audit_access_log_authority
        try:
            access = await run_sync_in_worker(
                authority.record_workflow_inspect,
                session_id=str(session_id),
                state_id=str(state_id),
                requesting_principal=user.user_id,
                ip_address=request.client.host if request.client else None,
            )
        except WorkflowInspectDenied:
            raise HTTPException(status_code=404, detail="Not found") from None
        except SQLAlchemyError as exc:
            raise AuditAccessLogWriteError("audit_access_log write failed for workflow inspect") from exc

        service: SessionServiceProtocol = request.app.state.session_service
        record = await service.get_state_in_session(state_id, session_id)
        state = state_from_record(record)
        reviews: RepositoryReviewAuthority = request.app.state.review_authority
        attestations = await run_sync_in_worker(reviews.attestations_for, session_id=str(session_id), state_id=str(state_id))

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return WorkflowInspectResponse(
            session_id=str(session_id),
            state_id=str(state_id),
            access_log_id=access.id,
            composition_snapshot=PublicCompositionStateResponse.model_validate(generate_public_composition_dict(state)),
            yaml=generate_public_yaml(state),
            attestations=[
                ReviewAttestationView(
                    attestation_id=item.attestation_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    payload_digest=item.payload_digest,
                    reviewer_identity_id=item.reviewer_identity_id,
                    author_identity_id=item.author_identity_id,
                    attested_at=item.attested_at,
                    verdict=item.verdict,
                    note=item.note,
                )
                for item in attestations
            ],
        )

    return router
```

- [ ] **Step 22: Run the inspect-route tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_workflow_inspect_routes.py -n 0 > /tmp/i7-lane-inspect-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. The empty seed is sufficient, measured on HEAD: `CompositionState.from_dict({"version": 1, "sources": {}, "nodes": [], "edges": [], "outputs": [], "metadata": {"name": "demo", "description": ""}})` validates through `shareable_reviews.models.CompositionStateResponse` as `version=1 metadata=PipelineMetadataResponse(name='demo', description='') sources={} nodes=[] edges=[] outputs=[]`, and `generate_public_yaml` returns `'{}\n'`.

- [ ] **Step 23: Write the failing audit-view route tests.**

Create `tests/unit/web/workflow/test_workflow_audit_view_routes.py`:

```python
"""GET /api/workflow/audit-view on I8's closed, governance-on local app.

Both halves are REAL. The Sessions half comes from RepositoryWorkflowScopeReader
over the fixture engine. The Landscape half is seeded into the file the
settings resolve (``data_dir/runs/audit.db``), through the Landscape's own
repositories, and read back by the route read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.workflow_scope_reader import RepositoryWorkflowScopeReader
from elspeth.web.sessions.models import (
    approvals_table,
    identity_relationships_table,
    identity_roles_table,
    review_attestations_table,
    sessions_table,
)
from elspeth.web.sessions.routes.workflow.audit_view import create_workflow_audit_view_router
from tests.fixtures.identities import ensure_test_identity

URL = "/api/workflow/audit-view"
T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def app(closed_local_app: Any, tmp_path: Path) -> Any:
    """boss (approver) oversees carol; zed is outside the tree. Each of carol and zed has a session, an approval, an attestation, a run and an auth event."""
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        for identity_id in ("boss", "carol", "zed", "rev"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(insert(identity_roles_table).values(role_id="role-boss", identity_id="boss", role="approver", granted_by_identity_id="alice", granted_at=T0))
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id="edge-boss-carol",
                from_identity_id="boss",
                to_identity_id="carol",
                relationship_type="approver",
                asserted_by_identity_id="alice",
                asserted_at=T0,
            )
        )
        for owner in ("carol", "zed"):
            conn.execute(insert(sessions_table).values(id=f"session-{owner}", user_id=owner, auth_provider_type="local", title=owner, created_at=T0, updated_at=T0))
            conn.execute(
                insert(approvals_table).values(
                    approval_id=f"ap-{owner}",
                    session_id=f"session-{owner}",
                    state_id=f"state-{owner}",
                    binding_json={},
                    requested_by_identity_id=owner,
                    approver_identity_id="boss",
                    requested_at=T0,
                )
            )
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=f"at-{owner}",
                    session_id=f"session-{owner}",
                    state_id=f"state-{owner}",
                    payload_digest="sha256:" + "0" * 64,
                    reviewer_identity_id="rev",
                    author_identity_id=owner,
                    attested_at=T0,
                    verdict="signed_off",
                )
            )
    (tmp_path / "runs").mkdir(exist_ok=True)
    with LandscapeDB.from_url(closed_local_app.app.state.settings.get_landscape_url(), create_tables=True) as db:
        factory = RecorderFactory(db)
        for owner in ("carol", "zed"):
            factory.run_lifecycle.begin_run(
                config={"pipeline": owner},
                canonical_version="v1",
                run_id=f"run-{owner}",
                initiated_by_user_id=owner,
                auth_provider_type="local",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
            factory.auth_audit.record_auth_event(
                event_type="role_granted",
                outcome="success",
                provider="local",
                user_id=owner,
                username=owner,
                failure_category=None,
                request_id=None,
                client_host=None,
                user_agent=None,
                metadata={"seed": owner},
                identity_id=owner,
            )
    closed_local_app.app.state.workflow_scope_reader = RepositoryWorkflowScopeReader(engine)
    closed_local_app.app.include_router(create_workflow_audit_view_router())
    return closed_local_app


def _as(client: Any, identity_id: str) -> None:
    identity = UserIdentity(user_id=identity_id, username=identity_id)

    async def user() -> UserIdentity:
        return identity

    client.app.dependency_overrides[get_current_user] = user


def test_governance_off_refuses_with_409(app: Any) -> None:
    _as(app, "boss")
    app.app.state.settings = app.app.state.settings.model_copy(update={"workflow_governance": "off"})

    response = app.get(URL)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_type"] == "workflow_governance_off"


def test_a_caller_without_the_approver_role_is_hidden(app: Any) -> None:
    _as(app, "carol")

    response = app.get(URL)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Not found"}


def test_the_view_holds_only_the_overseen_identities_across_both_stores(app: Any) -> None:
    _as(app, "boss")

    response = app.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity_ids"] == ["carol"]
    assert body["truncated"] is False
    assert [run["run_id"] for run in body["runs"]] == ["run-carol"]
    assert [run["initiated_by_identity_id"] for run in body["runs"]] == ["carol"]
    assert [approval["approval_id"] for approval in body["approvals"]] == ["ap-carol"]
    assert [attestation["attestation_id"] for attestation in body["attestations"]] == ["at-carol"]
    assert [(event["identity_id"], event["event_type"]) for event in body["auth_events"]] == [("carol", "role_granted")]
    assert "zed" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_revoking_the_edge_empties_every_half(app: Any) -> None:
    with app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identity_relationships_table).values(revoked_at=T0, revoked_by_identity_id="alice"))
    _as(app, "boss")

    body = app.get(URL).json()

    assert body == {"identity_ids": [], "truncated": False, "runs": [], "approvals": [], "attestations": [], "auth_events": []}


def test_a_missing_landscape_database_is_an_integrity_failure(app: Any) -> None:
    landscape_url = app.app.state.settings.get_landscape_url()
    Path(landscape_url.removeprefix("sqlite:///")).unlink()
    _as(app, "boss")

    with pytest.raises(AuditIntegrityError, match="Landscape audit database is missing"):
        app.get(URL)
```

- [ ] **Step 24: Run the audit-view tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_workflow_audit_view_routes.py -n 0 > /tmp/i7-lane-audit-view-red.log 2>&1; echo exit=$?`
Expected: `exit=2` (`Interrupted: 1 error during collection`), with `ModuleNotFoundError: No module named 'elspeth.web.sessions.routes.workflow.audit_view'`.

- [ ] **Step 25: Write the audit-view route.**

Create `src/elspeth/web/sessions/routes/workflow/audit_view.py`:

```python
"""GET /api/workflow/audit-view: the approver's audit view (sso-design.md:1378-1379).

``identity_relationships`` x ``run_attributions`` x ``auth_events``, plus the
approvals and attestations those identities produced, scoped to the caller's
ACTIVE approver edges to depth 8. The Sessions half comes from
``RepositoryWorkflowScopeReader``. The Landscape half is read HERE, read-only,
the ``sessions/routes/runs.py`` audit-story shape: Sessions-side modules never
open the Landscape.

A caller without a live ``approver`` grant sees 404. The view writes no
``audit_access_log`` row: that table's ``session_id`` is a NOT NULL foreign
key and this view spans sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table, run_attributions_table, runs_table
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.workflow_scope_reader import MAX_AUDIT_VIEW_ROWS, RepositoryWorkflowScopeReader
from elspeth.web.execution.discard_summary import _sqlite_database_file_missing

_RUNS_OF_SCOPE: Final = (
    select(
        run_attributions_table.c.run_id,
        run_attributions_table.c.initiated_by_user_id,
        run_attributions_table.c.recorded_at,
        runs_table.c.status,
        runs_table.c.started_at,
        runs_table.c.completed_at,
    )
    .select_from(run_attributions_table.join(runs_table, run_attributions_table.c.run_id == runs_table.c.run_id))
    .where(run_attributions_table.c.initiated_by_user_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(run_attributions_table.c.recorded_at.desc(), run_attributions_table.c.run_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)
_AUTH_EVENTS_OF_SCOPE: Final = (
    select(
        auth_events_table.c.event_id,
        auth_events_table.c.occurred_at,
        auth_events_table.c.event_type,
        auth_events_table.c.outcome,
        auth_events_table.c.identity_id,
        auth_events_table.c.metadata_json,
    )
    .where(auth_events_table.c.identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(auth_events_table.c.occurred_at.desc(), auth_events_table.c.event_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuditViewRun(_View):
    run_id: str
    initiated_by_identity_id: str
    recorded_at: datetime
    status: str
    started_at: datetime
    completed_at: datetime | None


class AuditViewApproval(_View):
    approval_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: str | None


class AuditViewAttestation(_View):
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: str


class AuditViewAuthEvent(_View):
    event_id: str
    occurred_at: datetime
    event_type: str
    outcome: str
    identity_id: str
    metadata_json: str


class WorkflowAuditViewResponse(_View):
    identity_ids: list[str]
    truncated: bool
    runs: list[AuditViewRun]
    approvals: list[AuditViewApproval]
    attestations: list[AuditViewAttestation]
    auth_events: list[AuditViewAuthEvent]


@dataclass(frozen=True, slots=True)
class _LandscapeHalf:
    runs: tuple[AuditViewRun, ...]
    auth_events: tuple[AuditViewAuthEvent, ...]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _read_landscape_half(settings: WebSettings, identity_ids: tuple[str, ...]) -> _LandscapeHalf:
    landscape_url = settings.get_landscape_url()
    if _sqlite_database_file_missing(landscape_url):
        # The web app creates the Landscape at startup (app.py, open_landscape_db),
        # so its absence is a Tier-1 integrity failure, not an empty audit trail.
        raise AuditIntegrityError(f"Landscape audit database is missing at {landscape_url}")
    parameters = {"identity_ids": list(identity_ids)}
    with (
        LandscapeDB.from_url(landscape_url, passphrase=settings.landscape_passphrase, create_tables=False, read_only=True) as db,
        db.read_only_connection() as conn,
    ):
        run_rows = conn.execute(_RUNS_OF_SCOPE, parameters).all()
        event_rows = conn.execute(_AUTH_EVENTS_OF_SCOPE, parameters).all()
    return _LandscapeHalf(
        runs=tuple(
            AuditViewRun(
                run_id=row.run_id,
                initiated_by_identity_id=row.initiated_by_user_id,
                recorded_at=_utc(row.recorded_at),
                status=row.status,
                started_at=_utc(row.started_at),
                completed_at=None if row.completed_at is None else _utc(row.completed_at),
            )
            for row in run_rows
        ),
        auth_events=tuple(
            AuditViewAuthEvent(
                event_id=row.event_id,
                occurred_at=_utc(row.occurred_at),
                event_type=row.event_type,
                outcome=row.outcome,
                identity_id=row.identity_id,
                metadata_json=row.metadata_json,
            )
            for row in event_rows
        ),
    )


def create_workflow_audit_view_router() -> APIRouter:
    router = APIRouter(tags=["workflow-audit-view"])

    @router.get("/api/workflow/audit-view", response_model=WorkflowAuditViewResponse)
    async def workflow_audit_view(
        request: Request,
        response: Response,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ) -> WorkflowAuditViewResponse:
        settings: WebSettings = request.app.state.settings
        if settings.workflow_governance != "on":
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "workflow_governance_off",
                    "detail": "workflow governance is off on this deployment (set ELSPETH_WEB__WORKFLOW_GOVERNANCE=on)",
                },
            )
        reader: RepositoryWorkflowScopeReader = request.app.state.workflow_scope_reader
        scope = await run_sync_in_worker(reader.read_audit_scope, caller=user.user_id)
        if not scope.caller_is_approver:
            raise HTTPException(status_code=404, detail="Not found")
        landscape = (
            _LandscapeHalf(runs=(), auth_events=())
            if not scope.identity_ids
            else await run_sync_in_worker(_read_landscape_half, settings, scope.identity_ids)
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return WorkflowAuditViewResponse(
            identity_ids=list(scope.identity_ids),
            truncated=scope.truncated,
            runs=list(landscape.runs),
            approvals=[
                AuditViewApproval(
                    approval_id=item.approval_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    requested_by_identity_id=item.requested_by_identity_id,
                    approver_identity_id=item.approver_identity_id,
                    requested_at=item.requested_at,
                    decided_at=item.decided_at,
                    decision=item.decision,
                )
                for item in scope.approvals
            ],
            attestations=[
                AuditViewAttestation(
                    attestation_id=item.attestation_id,
                    session_id=item.session_id,
                    state_id=item.state_id,
                    payload_digest=item.payload_digest,
                    reviewer_identity_id=item.reviewer_identity_id,
                    author_identity_id=item.author_identity_id,
                    attested_at=item.attested_at,
                    verdict=item.verdict,
                )
                for item in scope.attestations
            ],
            auth_events=list(landscape.auth_events),
        )

    return router
```

- [ ] **Step 26: Run the audit-view tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/workflow/test_workflow_audit_view_routes.py -n 0 > /tmp/i7-lane-audit-view-green.log 2>&1; echo exit=$?`
Expected: `exit=0`. If the fixture's `begin_run` refuses the seed (for example with `AuditIntegrityError` naming `openrouter_catalog_source`), copy the accepted source value from `tests/unit/core/landscape/test_run_lifecycle_repository.py:1683-1684` into the fixture.

- [ ] **Step 27: Wire the reader and both routers into the app.**

In `src/elspeth/web/app.py`, directly after `from elspeth.web.coordination.websocket_ticket_authority import RepositorySessionWebsocketTicketAuthority` (:107):

```python
from elspeth.web.coordination.workflow_scope_reader import RepositoryWorkflowScopeReader
```

Beside I5's `from elspeth.web.sessions.routes.workflow.library import create_library_router` (on your branch, near `from elspeth.web.shareable_reviews.routes import create_shareable_reviews_router` at :164):

```python
from elspeth.web.sessions.routes.workflow.audit_view import create_workflow_audit_view_router
from elspeth.web.sessions.routes.workflow.inspect import create_workflow_inspect_router
```

Directly after `app.state.audit_access_log_authority = audit_access_log_authority` (:1586):

```python
    # --- Workflow scope reader (I7) ---
    # Read-only Sessions half of the approver's audit view; the route reads
    # the Landscape half itself.
    app.state.workflow_scope_reader = RepositoryWorkflowScopeReader(session_engine)
```

Directly after I5's `app.include_router(create_library_router())`, which follows `app.include_router(create_shareable_reviews_router())` (:1791):

```python
    app.include_router(create_workflow_inspect_router())
    app.include_router(create_workflow_audit_view_router())
```

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_app.py -n 0 > /tmp/i7-lane-app.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 28: Admit the new writers and the new read connection to the Sessions mutation-authority manifest.**

First read the drift report. The gate XFAILs on drift instead of failing:

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -v -rx > /tmp/i7-lane-manifest-drift.log 2>&1; echo exit=$?`
Expected: `exit=0` and `1 xfailed`. The reason text lists, one `describe()` line each (`<path>:<line> <symbol> <operation> <table> fp=<16 hex>#<ordinal> authority=<name or UNCLASSIFIED> connection_escape=False`):
`src/elspeth/web/coordination/audit_access_log_authority.py:<line> RepositoryAuditAccessLogAuthority.record_workflow_inspect insert audit_access_log`;
`src/elspeth/web/coordination/identity_authority.py:<line> RepositoryIdentityAuthority.grant_curator_as_approver insert identity_roles`, the same symbol with `update identity_roles`, and the same symbol with `write_connection <sessions-write-connection>`;
`src/elspeth/web/coordination/workflow_scope_reader.py:<line> RepositoryWorkflowScopeReader.read_audit_scope write_connection <sessions-write-connection>` under "Connections outside exact contained authority".
An `Unresolved write executions` count above zero means an execute on a contained connection is not a module-level statement the scanner resolves. Fix the module, not the manifest.

The log will ALSO list existing rows under `Stale reviewed`, each with its twin under `Unexpected/unreviewed`: `line` is part of the identity key (`_identity_key`, :10512-10523), so every reviewed row below an insert point in the same file moves. That means the `record_audit_grade_view` row (`line=102`), because Step 8 lengthens the module head above it, and every `RepositoryIdentityAuthority` row whose `line=` is greater than 2646 in `identity_authority.py` (`revoke_role`, `assert_relationship`, `revoke_relationship`, `purge_stale_pending_identities` and the rest). For each such pair, update only the `line=` value from the log. The fingerprint must be unchanged; if it differs, you edited that method, so revert the edit.

Measured on HEAD: neither `_REVIEWED_NON_SESSION_CONNECTIONS` (:4663) nor `test_web_landscape_mutation_fencing.py` names `sessions/routes/runs.py` or `execution/accounting.py`, the two web modules that already open the Landscape with `read_only=True`, so a read-only Landscape open in a web module is not inventoried today. If the drift log nevertheless lists `src/elspeth/web/sessions/routes/workflow/audit_view.py:<line> _read_landscape_half write_connection <non-session-write-connection>`, add that row to `_REVIEWED_NON_SESSION_CONNECTIONS` in the shape of its `RunLifecycleRepository.begin_run` row (:4666-4676), copying `fingerprint`, `line` and `connection_escape` from the log, and raise the count pin `assert len(_REVIEWED_NON_SESSION_CONNECTIONS) == 56` (:18037) to 57 in the same edit.

Then edit `tests/unit/architecture/test_session_db_mutation_authority.py`:

1. `_NAMED_AUTHORITY_SYMBOLS`: directly after the `RepositoryAuditAccessLogAuthority.record_audit_grade_view` entry (:429-433), add

```python
    AuthoritySymbol(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_workflow_inspect",
        "AuditAccessLogAuthority",
    ),
```

and directly after the `RepositoryIdentityAuthority.revoke_role` entry (:868-872), add

```python
    # I7 delegated administration: an approver appoints a curator over a
    # directly overseen identity; the edge and role are re-proved in-method.
    AuthoritySymbol(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "IdentityAuthority",
    ),
```

2. `_CONTAINED_CONNECTION_AUTHORITIES`: the same two `AuthoritySymbol` blocks, the first after the `record_audit_grade_view` entry (:1032-1036), the second after the `RepositoryIdentityAuthority.revoke_role` entry (:1181-1185).

3. `_REVIEWED_WRITERS`: directly after the `record_audit_grade_view` row (:2053-2062), add

```python
    # I7 D27: one row per authorized workflow read, written only after the
    # per-request predicate holds inside the session lock.
    WriterIdentity(
        "src/elspeth/web/coordination/audit_access_log_authority.py",
        "RepositoryAuditAccessLogAuthority.record_workflow_inspect",
        "audit_access_log",
        "insert",
        "FP_FROM_LOG",
        1,
        "AuditAccessLogAuthority",
        line=LINE_FROM_LOG,
    ),
```

and directly after the `RepositoryIdentityAuthority.revoke_role` `identity_roles` `update` row (:3145-3154), add

```python
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "identity_roles",
        "insert",
        "FP_FROM_LOG",
        1,
        "IdentityAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "identity_roles",
        "update",
        "FP_FROM_LOG",
        1,
        "IdentityAuthority",
        line=LINE_FROM_LOG,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/identity_authority.py",
        "RepositoryIdentityAuthority.grant_curator_as_approver",
        "<sessions-write-connection>",
        "write_connection",
        "FP_FROM_LOG",
        1,
        "IdentityAuthority",
        line=LINE_FROM_LOG,
    ),
```

4. `_REVIEWED_READ_CONNECTIONS` (:3954): after the `RepositoryIdentityAuthority.configured_admin_seed_consumed` row (:3957-3966), add

```python
    # I7 approver audit view: SELECT-only scope walk and scoped reads; the
    # connection never leaves the method and the Landscape half is the route's.
    WriterIdentity(
        "src/elspeth/web/coordination/workflow_scope_reader.py",
        "RepositoryWorkflowScopeReader.read_audit_scope",
        "<sessions-write-connection>",
        "write_connection",
        "FP_FROM_LOG",
        1,
        None,
        line=LINE_FROM_LOG,
    ),
```

In every `FP_FROM_LOG` / `LINE_FROM_LOG` slot, write the fingerprint, ordinal and line for that exact symbol and operation, copied from `/tmp/i7-lane-manifest-drift.log`. The fingerprint is an AST hash of the method as written on your branch, so it is measured there and never typed from this plan.

5. Replace the body of `test_audit_access_log_writer_is_exactly_bound_to_its_handle_free_authority` (:11023-11040) with:

```python
def test_audit_access_log_writer_is_exactly_bound_to_its_handle_free_authority() -> None:
    root = _repo_root()
    repository_path = "src/elspeth/web/coordination/audit_access_log_authority.py"
    authority_symbols = (
        "RepositoryAuditAccessLogAuthority.record_audit_grade_view",
        "RepositoryAuditAccessLogAuthority.record_workflow_inspect",
    )
    for authority_symbol in authority_symbols:
        assert _authority_for(repository_path, authority_symbol) == "AuditAccessLogAuthority"
        assert _contained_connection_authority_for(repository_path, authority_symbol) == "AuditAccessLogAuthority"
        assert _authority_for(repository_path, f"{authority_symbol}_replacement") is None
    assert _authority_for(repository_path, "RepositoryAuditAccessLogAuthority.future_method") is None

    paths = [
        root / repository_path,
        root / "src/elspeth/web/sessions/service.py",
    ]
    live = [site for site in scan_production_writers(paths, anchor=root) if site.table == "audit_access_log"]
    reviewed = [site for site in _REVIEWED_WRITERS if site.table == "audit_access_log"]
    assert len(live) == len(reviewed) == 2
    assert sorted(site.symbol for site in live) == sorted(authority_symbols)
    assert inventory_drift(live, reviewed) == ([], [])
```

Never widen a `TablePolicy`: `audit_access_log` → `AuditAccessLogAuthority` (:122) and `identity_roles` → `IdentityAuthority` (:112) already name these owners. `test_named_authority_registry_is_explicit_extensible_and_exact` (:10938-11021) pins no count of `_NAMED_AUTHORITY_SYMBOLS`; its last assertion only requires every bound authority to appear in `_TABLE_POLICIES`, which both new bindings satisfy.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -v -rx > /tmp/i7-lane-manifest-green.log 2>&1; echo exit=$?`
Expected: `exit=0`, and the short summary lists no `XFAIL test_all_production_sessions_writers_are_reviewed_typed_authorities`. Control the instrument: change one character of the `record_workflow_inspect` fingerprint and re-run the gate id with `-rx`. Confirm `1 xfailed` returns with `Stale reviewed (1)` naming that row, then restore the character and re-run to `exit=0`.

- [ ] **Step 29: Run the PostgreSQL proofs.**

Create `tests/testcontainer/web/test_workflow_inspect_postgres.py`:

```python
"""PostgreSQL proofs for I7's workflow_inspect writer and audit-scope reader.

SQLite drops FOR UPDATE and serialises writers. These prove, on the dialect
production runs, the session-row lock path, the correlated NOT EXISTS over
timestamptz, the expanding IN, and the closed writer_principal CHECK.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import make_url

from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.workflow_scope_reader import RepositoryWorkflowScopeReader
from elspeth.web.sessions import models
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import WorkflowInspectDenied
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity

pytestmark = pytest.mark.testcontainer

T0 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def workflow_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"workflow_inspect_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as conn:
            for identity_id in ("alice", "bob", "carol", "dave"):
                ensure_test_identity(conn, identity_id=identity_id)
            conn.execute(insert(models.sessions_table).values(id=SESSION, user_id="alice", auth_provider_type="local", title="pg", created_at=T0, updated_at=T0))
            conn.execute(
                insert(models.composition_states_table).values(
                    id=STATE,
                    session_id=SESSION,
                    version=1,
                    provenance="session_seed",
                    created_at=T0,
                    sources={},
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "demo", "description": ""},
                )
            )
            for role_id, identity_id, role in (("role-bob", "bob", "approver"), ("role-carol", "carol", "reviewer")):
                conn.execute(
                    insert(models.identity_roles_table).values(role_id=role_id, identity_id=identity_id, role=role, granted_by_identity_id="alice", granted_at=T0)
                )
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def test_an_authorised_inspect_commits_one_row_under_the_postgres_check(workflow_engine: Engine) -> None:
    with workflow_engine.begin() as conn:
        conn.execute(
            insert(models.approvals_table).values(
                approval_id="ap-1",
                session_id=SESSION,
                state_id=STATE,
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="bob",
                requested_at=T0,
            )
        )

    record = RepositoryAuditAccessLogAuthority(workflow_engine).record_workflow_inspect(
        session_id=SESSION, state_id=STATE, requesting_principal="bob", ip_address="192.0.2.1"
    )

    with workflow_engine.connect() as conn:
        rows = conn.execute(select(models.audit_access_log_table)).all()
    assert [(row.id, row.writer_principal, row.query_args) for row in rows] == [(record.id, "workflow_inspect", {})]


def test_the_reviewer_arm_closes_on_a_later_attestation_on_postgres(workflow_engine: Engine) -> None:
    with workflow_engine.begin() as conn:
        conn.execute(
            insert(models.review_requests_table).values(
                request_id="rr-1", session_id=SESSION, state_id=STATE, requested_by_identity_id="alice", reviewer_identity_id=None, requested_at=T0
            )
        )
    authority = RepositoryAuditAccessLogAuthority(workflow_engine)
    authority.record_workflow_inspect(session_id=SESSION, state_id=STATE, requesting_principal="carol", ip_address=None)

    with workflow_engine.begin() as conn:
        conn.execute(
            insert(models.review_attestations_table).values(
                attestation_id="at-1",
                session_id=SESSION,
                state_id=STATE,
                payload_digest="sha256:" + "0" * 64,
                reviewer_identity_id="dave",
                author_identity_id="alice",
                attested_at=T0 + timedelta(hours=1),
                verdict="signed_off",
            )
        )
    with pytest.raises(WorkflowInspectDenied, match="no live approval or review request"):
        authority.record_workflow_inspect(session_id=SESSION, state_id=STATE, requesting_principal="carol", ip_address=None)

    with workflow_engine.connect() as conn:
        assert len(conn.execute(select(models.audit_access_log_table)).all()) == 1


def test_the_audit_scope_reader_walks_a_seeded_cycle_on_postgres(workflow_engine: Engine) -> None:
    with workflow_engine.begin() as conn:
        for from_id, to_id in (("bob", "carol"), ("carol", "dave"), ("dave", "bob")):
            conn.execute(
                insert(models.identity_relationships_table).values(
                    relationship_id=f"edge-{from_id}-{to_id}",
                    from_identity_id=from_id,
                    to_identity_id=to_id,
                    relationship_type="approver",
                    asserted_by_identity_id="alice",
                    asserted_at=T0,
                )
            )
        conn.execute(
            insert(models.approvals_table).values(
                approval_id="ap-dave",
                session_id=SESSION,
                state_id="state-dave",
                binding_json={},
                requested_by_identity_id="dave",
                approver_identity_id="bob",
                requested_at=T0,
            )
        )

    scope = RepositoryWorkflowScopeReader(workflow_engine).read_audit_scope(caller="bob")

    assert scope.identity_ids == ("carol", "dave")
    assert [row.approval_id for row in scope.approvals] == ["ap-dave"]
```

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_workflow_inspect_postgres.py tests/testcontainer/web/test_identity_owner_schema_postgres.py tests/testcontainer/web/test_session_derived_mutations_postgres.py -m testcontainer -n 0 > /tmp/i7-lane-pg.log 2>&1; echo exit=$?`
Expected: `exit=0` (needs Docker). Without `-m testcontainer` the selection is empty and pytest exits 5. The two existing files are the PostgreSQL suites that already import `RepositoryAuditAccessLogAuthority`. They must stay green after the import and constant additions to its module.

- [ ] **Step 30: Run the neighbouring whole-tree gates, lint, type-check and the corpus diff.**

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py tests/unit/architecture/test_web_landscape_mutation_fencing.py tests/unit/web/coordination tests/unit/web/workflow tests/unit/web/auth tests/unit/web/sessions/test_protocol.py tests/unit/web/sessions/test_record_audit_grade_view.py -n 0 > /tmp/i7-lane-neighbours.log 2>&1; echo exit=$?`
Expected: `exit=0`. The Landscape fencing file may report its declared burn-down `xfail`s.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python scripts/fencing_inventory.py . > /tmp/i7-lane-fencing.log 2>&1; echo exit=$?`
Expected: `exit=0`, with every `PINNED INVENTORIES` row reading `ok`. Measured on HEAD 072141b75: `dml_identities live= 158 pinned= 158`, `production_calls live= 280 pinned= 280`, `coordination_calls live= 43 pinned= 43`, `internal_edges live= 92 pinned= 92`, `subordinate_edges live= 138 pinned= 138`, every violation set `0`. Those pins count mutation-API callers and DML. The existing read-only openers `sessions/routes/runs.py:182` and `execution/accounting.py:80` appear in none of them, so `audit_view.py` must leave every count unchanged. A moved count naming `audit_view.py` means the read reached a mutation API. Fix the route, never the pin.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python -m scripts.check_contracts > /tmp/i7-lane-contracts.log 2>&1; echo exit=$?`
Expected: `exit=0`. Nothing in this task annotates a `dict`/`Mapping` of `str` to `Any`/`object`. A census drift names the file and form: rewrite that annotation as an owned type rather than re-pinning.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check --fix src/elspeth/web/coordination/audit_access_log_authority.py src/elspeth/web/coordination/workflow_scope_reader.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/auth/identity_admin_routes.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/routes/workflow/inspect.py src/elspeth/web/sessions/routes/workflow/audit_view.py src/elspeth/web/app.py tests/unit/web/coordination/test_workflow_inspect_authority.py tests/unit/web/coordination/test_workflow_scope_reader.py tests/unit/web/workflow/test_workflow_inspect_routes.py tests/unit/web/workflow/test_workflow_audit_view_routes.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_protocol.py tests/unit/architecture/test_session_db_mutation_authority.py tests/testcontainer/web/test_workflow_inspect_postgres.py > /tmp/i7-lane-ruff.log 2>&1; echo exit=$? && ruff format src/elspeth/web/coordination/audit_access_log_authority.py src/elspeth/web/coordination/workflow_scope_reader.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/auth/identity_admin_routes.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/routes/workflow/inspect.py src/elspeth/web/sessions/routes/workflow/audit_view.py src/elspeth/web/app.py tests/unit/web/coordination/test_workflow_inspect_authority.py tests/unit/web/coordination/test_workflow_scope_reader.py tests/unit/web/workflow/test_workflow_inspect_routes.py tests/unit/web/workflow/test_workflow_audit_view_routes.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_protocol.py tests/unit/architecture/test_session_db_mutation_authority.py tests/testcontainer/web/test_workflow_inspect_postgres.py >> /tmp/i7-lane-ruff.log 2>&1; echo exit=$?`
Expected: `exit=0` twice. If a formatter change touched the manifest file, re-run Step 28's green command, because fingerprints hash the AST, not the whitespace, but the `line=` values can move.

Run: `cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/web/coordination/audit_access_log_authority.py src/elspeth/web/coordination/workflow_scope_reader.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/auth/identity_admin_routes.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/routes/workflow/inspect.py src/elspeth/web/sessions/routes/workflow/audit_view.py src/elspeth/web/app.py > /tmp/i7-lane-mypy.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing .venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth > /tmp/i7-lane-lints-after.log 2>&1; echo exit=$? && diff /tmp/i7-lane-lints-before.log /tmp/i7-lane-lints-after.log > /tmp/i7-lane-lints.diff; echo diff_exit=$?`
Expected: `exit=1` (the standing corpus). Read `/tmp/i7-lane-lints.diff`: a finding line present only in the after-log that names `workflow_scope_reader.py`, `inspect.py`, `audit_view.py`, or the new methods in `audit_access_log_authority.py` / `identity_authority.py` is a finding this task added. Fix the code, and never add a suppression. Lines that only moved within `identity_authority.py` because of the inserted method are not new findings.

- [ ] **Step 31: Add the changelog line.**

In `CHANGELOG.md`, under `## 0.8.1 - 2026-09-10`, after the workflow bullets I8/I3/I4/I5/I6 added below `**Authentication events in signed exports.**` (:35-38):

```markdown
- **Scoped workflow reads.** While an open approval or review request is
  addressed to them, an approver or reviewer can read the public projection of
  another person's composition state. Each read writes an `audit_access_log`
  row under `workflow_inspect` and mints no share token. An approver's audit
  view lists the runs, approvals, attestations and authentication events of
  the people they oversee, up to eight levels down, and an approver may
  appoint a curator over someone they directly oversee. All three require
  `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`.
```

- [ ] **Step 32: Branch safety, then commit by file pathspec.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit > /tmp/i7-lane-branch-safety.log 2>&1; echo exit=$?`
Expected: `exit=0` with no `[FAIL]` line. Then `git status --short` and confirm that only the files below are modified or new for this task.

The eight files this task creates are untracked, and `git commit -- <pathspec>` aborts with `pathspec ... did not match any file(s) known to git` on an untracked path. Record intent-to-add for exactly those eight (this stages no content, so a sibling session's index entries are untouched):

```bash
git add -N -- src/elspeth/web/coordination/workflow_scope_reader.py src/elspeth/web/sessions/routes/workflow/inspect.py src/elspeth/web/sessions/routes/workflow/audit_view.py tests/unit/web/coordination/test_workflow_inspect_authority.py tests/unit/web/coordination/test_workflow_scope_reader.py tests/unit/web/workflow/test_workflow_inspect_routes.py tests/unit/web/workflow/test_workflow_audit_view_routes.py tests/testcontainer/web/test_workflow_inspect_postgres.py
```

Run: `git status --short` and confirm each of those paths now shows ` A` (intent-to-add), then commit:

```bash
git commit -m "feat(identity): workflow inspect, approver audit view, delegated administration" -- src/elspeth/web/sessions/protocol.py src/elspeth/web/coordination/audit_access_log_authority.py src/elspeth/web/coordination/workflow_scope_reader.py src/elspeth/web/coordination/identity_authority.py src/elspeth/web/auth/identity_admin_routes.py src/elspeth/web/sessions/routes/workflow/inspect.py src/elspeth/web/sessions/routes/workflow/audit_view.py src/elspeth/web/app.py tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/sessions/test_protocol.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/coordination/test_workflow_inspect_authority.py tests/unit/web/coordination/test_workflow_scope_reader.py tests/unit/web/workflow/test_workflow_inspect_routes.py tests/unit/web/workflow/test_workflow_audit_view_routes.py tests/testcontainer/web/test_workflow_inspect_postgres.py CHANGELOG.md
```

Check that `git show --stat HEAD` lists exactly these 17 files.
